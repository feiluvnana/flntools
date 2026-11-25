"""The library (`_library.open`, `Flnd`, `Run`, events), end to end over fakes. No network, no browser.

The gate of the library is that every command of today's CLI can be done through it, so
`CliCommandsTest` has one test per command, each doing what the command does with the library
alone. The other classes test a `Run` (lazy start, the warning when it is never started, stop,
async), the events and the report, the reads, and `when`.

Every test runs on `EngineCase`: the fake network answers from `self.routes`, the clock is fake,
and the home is a temporary folder.
"""

import asyncio
import datetime
import gc
import sqlite3
import threading
import unittest
import warnings
from pathlib import Path
from unittest import mock

from fakes import CDN, Engine, EngineCase, drive, login_cookies, make_plugin, noise, sign_in

from flnd import _library, errors, plugin
from flnd.engine import events as _events
from flnd.engine import jobs as jobs_module
from flnd.engine import store as _store
from flnd.engine.plugins import PLUGINS
from flnd.plugin import File, Plugin, Post, Target, route
from flnkit import files as _files
from flnkit import net as _net

JPEG = noise("JPEG")


def example_source(listing, *, code="exs", **declared):
    """A plugin with two categories and single posts, listing what `listing` holds.

    Args:
        listing: `{(user, category): [post ids]}`; each post has one file, `<CDN>/<id>.jpg`.
        code: the plugin's code; its domain is `<code>.example`.
        **declared: the class's other fields, such as `login`.
    """
    def posted(post_id):
        return Post(post_id, [File(f"{CDN}/{post_id}.jpg")], date=datetime.date(2026, 3, 1))

    @route(f"{code}:p/{{id}}", newest_first=False, single=True)
    async def post(self, ctx, id):
        yield posted(id)

    @route(f"{code}:{{user}}", newest_first=False)
    async def user(self, ctx, user):
        return user

    @user.category(newest_first=False)
    async def posts(self, ctx, user):
        for post_id in listing.get((user, "posts"), []):
            yield posted(post_id)

    @user.category(newest_first=False)
    async def reels(self, ctx, user):
        for post_id in listing.get((user, "reels"), []):
            yield posted(post_id)

    attributes = {"code": code, "name": "Example", "domains": (f"{code}.example",), "api": 2,
                  "folder": "{category}", "key": "{post}", "filename": "{date}_{post}{index}",
                  "post": post, "user": user, "posts": posts, "reels": reels, **declared}
    return type("Example", (Plugin,), attributes)


def registered(plugin):
    """Put a plugin in the built-in registry for one test, as `Flnd.add` once did."""
    return mock.patch.dict(PLUGINS, {plugin.code: plugin})


class LibraryCase(EngineCase):
    """A home with the example source added, and every `<CDN>/<id>.jpg` a small JPEG."""

    listing = {("example_user", "posts"): ["p1", "p2"], ("example_user", "reels"): ["r1"]}

    def setUp(self):
        super().setUp()
        self.listing = {key: list(ids) for key, ids in type(self).listing.items()}
        self.source = example_source(self.listing)
        self.fl = self.enterContext(_library.open(self.home))
        self.enterContext(registered(self.source))

    def serve(self, request):
        if str(request.url).startswith(CDN):
            self.requests.append(request)
            return _net.reply(200, content=JPEG)
        return super().serve(request)

    def saved(self, events):
        return sorted(event.post for event in events if isinstance(event, _events.Saved))


class GetTest(LibraryCase):
    """`get`, and each group of the library (SPEC.md#for-developers), as the commands use them."""

    def test_get_runs_and_follows_the_target(self):
        report = self.fl.get("exs:example_user").wait()
        self.assertEqual((report.saved, report.ok), (3, True))
        self.assertEqual(report.bytes, 3 * len(JPEG))
        [tracked] = self.fl.targets
        self.assertEqual(tracked.target, plugin.Target("exs:example_user", kind="user"))
        self.assertEqual((tracked.files, tracked.follow), (3, True))
        self.assertEqual(tracked.categories, ("posts", "reels"))
        self.assertEqual(tracked.complete, {"posts": tracked.complete["posts"],
                                            "reels": tracked.complete["reels"]})

    def test_get_with_categories_when_and_limit(self):
        run = self.fl.get("exs:example_user|reels", when="2026", limit=5)
        self.assertEqual(self.saved(run), ["r1"])

    def test_get_dry_plans_and_writes_nothing(self):
        planned = list(self.fl.get("exs:example_user", dry=True))
        self.assertEqual(sorted((event.post, event.state) for event in planned),
                         [("p1", "new"), ("p2", "new"), ("r1", "new")])
        self.assertFalse((self.home / "media").exists())

    def test_get_with_no_target_syncs_the_followed_ones(self):
        self.fl.get("exs:example_user").wait()
        self.listing["example_user", "posts"].insert(0, "p0")
        self.assertEqual(self.saved(self.fl.get()), ["p0"])
        self.assertEqual(self.fl.get("exs:example_user").wait().saved, 0)

    def test_get_with_folder_template_and_validation(self):
        saved = list(self.fl.get("exs:example_user|posts", folder="custom/{target}/{date}"))
        root = self.home / "media" / "exs"
        self.assertEqual(
            sorted(e.path.relative_to(root).as_posix() for e in saved if isinstance(e, _events.Saved)),
            ["custom/example_user/2026-03-01/2026-03-01_p1.jpg",
             "custom/example_user/2026-03-01/2026-03-01_p2.jpg"])

        # Dry run reflects the custom folder path and writes nothing.
        planned = list(self.fl.get("exs:example_user|reels", folder="preview/{target}", dry=True))
        self.assertEqual(
            [e.path.relative_to(root).as_posix() for e in planned],
            ["preview/example_user/2026-03-01_r1.jpg"])
        self.assertFalse((root / "preview").exists())

        # No targets with folder= runs followed targets into the custom folder.
        self.listing["example_user", "posts"].insert(0, "p0")
        synced = list(self.fl.get(folder="synced"))
        self.assertEqual(
            [e.path.relative_to(root).as_posix() for e in synced if isinstance(e, _events.Saved)],
            ["synced/2026-03-01_p0.jpg"])

        # Invalid folder templates raise UsageError before any request is made.
        self.requests.clear()
        for bad in ("..", "a/../b", "/abs", "\\back", "C:drive", "a\x00b", "{nope}",
                    "{bad", "bad}", "{}"):
            with self.subTest(bad=bad), self.assertRaises(errors.UsageError):
                self.fl.get("exs:example_user", folder=bad)
        self.assertEqual(self.requests, [])

    def test_a_single_post_is_never_followed(self):
        self.fl.get("exs:p/9").wait()
        [tracked] = self.fl.targets
        self.assertFalse(tracked.follow)
        with self.assertRaises(errors.UsageError):
            self.fl.targets.follow("exs:p/9")

    def test_follow_unfollow_and_retry(self):
        [tracked] = self.fl.targets.follow("exs:example_user")
        self.assertTrue(tracked.follow)
        self.fl.targets.unfollow("exs:example_user")
        self.assertFalse(list(self.fl.targets)[0].follow)
        self.assertEqual(self.fl.get().wait().saved, 0)
        self.fl.get("exs:example_user").wait()
        self.assertEqual(len(self.fl.targets.retry("exs:example_user")), 1)
        with self.assertRaises(errors.UsageError):
            self.fl.targets.unfollow("exs:example_creator")

    def test_clean_forgets_the_target_and_with_media_its_files(self):
        self.fl.get("exs:example_user").wait()
        [forgotten] = self.fl.targets.clean("exs:example_user")
        self.assertEqual(forgotten.files, 3)
        self.assertEqual((list(self.fl.targets), self.fl.files()), ([], []))
        self.assertTrue(any((self.home / "media").rglob("*.jpg")))
        self.fl.get("exs:example_user").wait()
        self.fl.targets.clean("exs:example_user", media=True)
        self.assertFalse(any((self.home / "media").rglob("*.jpg")))
        self.assertFalse((self.home / "media" / "exs").exists())

    def test_auth_login_logout(self):
        self.enterContext(registered(example_source({}, code="exc", login=login_cookies("sid"))))
        cookies = Path(self.home.parent / "cookies.txt")
        cookies.write_text("# Netscape HTTP Cookie File\n"
                           ".exc.example\tTRUE\t/\tTRUE\t1825861327\tsid\texample-value\n",
                           encoding="utf-8")
        first = self.fl.auth.login("exc", method="file", path=str(cookies))
        second = self.fl.auth.login("exc", path=str(cookies))
        self.assertEqual((first.plugin, first.status, second.file), ("exc", "ok", first.file))
        self.assertEqual([item.plugin for item in self.fl.auth], ["exc"])
        self.assertEqual(self.fl.auth.logout("exc").plugin, "exc")
        self.assertEqual(list(self.fl.auth), [])
        for call in (lambda: self.fl.auth.logout("exc"),
                     lambda: self.fl.auth.login("exc", method="nope"),
                     lambda: self.fl.auth.login("exs")):
            with self.subTest(), self.assertRaises(errors.UsageError):
                call()

    def test_config_reads_each_key_with_its_default_and_origin(self):
        self.assertEqual((self.fl.config["when"], self.fl.config.where("when")), ("", "default"))
        self.assertEqual(self.fl.config.default("when"), "")
        with self.assertRaises(errors.UsageError):
            self.fl.config["nope"]


class RunTest(LibraryCase):
    def test_nothing_happens_until_the_run_is_read(self):
        run = self.fl.get("exs:example_user")
        self.assertEqual((self.requests, run.report), ([], _events.Report()))
        self.assertIn("not started", repr(run))
        run.wait()
        self.assertEqual(len(self.requests), 3)

    def test_an_unstarted_run_warns_when_dropped(self):
        run = self.fl.get("exs:example_user")
        with self.assertWarns(ResourceWarning) as caught:
            del run
            gc.collect()
        self.assertIn("never started", str(caught.warning))

    def test_a_started_or_stopped_run_does_not_warn(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            self.fl.get("exs:example_user").wait()
            unstarted = self.fl.get("exs:example_user")
            unstarted.stop()
            self.assertTrue(unstarted.wait().stopped)
            del unstarted
            gc.collect()

    def test_breaking_out_pauses_and_the_report_holds_the_counts_so_far(self):
        run = self.fl.get("exs:example_user")
        for event in run:
            if isinstance(event, _events.Saved):
                break
        self.assertGreaterEqual(run.report.saved, 1)
        self.assertEqual(run.wait().saved, 3)
        self.assertEqual(list(run), [])

    def test_a_reader_that_stops_reading_never_holds_up_another_files_transfer(self):
        # E5: the files' events come from the run's loop; with the queue full they wait aside,
        # so the other files go on while the reader holds the first event.
        from flnd.engine import run as _run

        self.enterContext(mock.patch.object(_run, "QUEUE", 1))
        self.listing["example_user", "posts"] = [f"p{n}" for n in range(6)]
        run = self.fl.get("exs:example_user|posts")
        landed, real = threading.Event(), run._emit

        def emit(event):
            done = real(event)
            if run.report.saved == 6:
                landed.set()
            return done

        run._emit = emit
        events = iter(run)
        first = next(event for event in events if isinstance(event, _events.Saved))
        self.assertTrue(landed.wait(30), "the other files waited for the reader")
        self.assertEqual(len([first, *(e for e in events if isinstance(e, _events.Saved))]), 6)

    def test_stop_ends_the_run_without_an_error(self):
        # One place, so most files are still queued when the first lands: a stop drops those and
        # finishes only what is downloading. At the default of 32 places all 30 would be in flight.
        self.enterContext(mock.patch.object(jobs_module, "PLACES", 1))
        self.listing["example_user", "posts"] = [f"p{n}" for n in range(30)]
        run = self.fl.get("exs:example_user|posts")
        for event in run:
            if isinstance(event, _events.Saved):
                run.stop()
        report = run.report
        self.assertTrue(report.stopped)
        self.assertLess(report.saved, 30)

    def test_ctrl_c_while_reading_stops_the_run_and_raises_stopped(self):
        import signal

        # One place, as above: at 32 places every file could be in flight before the first lands.
        self.enterContext(mock.patch.object(jobs_module, "PLACES", 1))
        self.listing["example_user", "posts"] = [f"p{n}" for n in range(30)]
        run = self.fl.get("exs:example_user|posts")
        with self.assertRaises(errors.Stopped):
            for event in run:
                if isinstance(event, _events.Saved) and run.report.saved == 1:
                    # The reader's handler takes it: the first Ctrl-C only stops listing.
                    signal.raise_signal(signal.SIGINT)
        self.assertFalse(run.report.stopped)
        self.assertLess(run.report.saved, 30)
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)

    def test_a_third_ctrl_c_puts_the_terminal_back_and_exits_at_once(self):
        import signal

        from flnd.engine import run as _run
        from flnd.ui.log import ui

        class Quit(BaseException):
            """What the patched `os._exit` raises, so the test sees the exit instead of dying."""

        undone = []
        self.enterContext(mock.patch.object(jobs_module, "PLACES", 1))
        self.listing["example_user", "posts"] = [f"p{n}" for n in range(30)]
        warn = self.enterContext(mock.patch.object(ui, "warn"))
        run = self.fl.get("exs:example_user|posts")
        with mock.patch.object(_run.os, "_exit", side_effect=Quit) as exit_now, \
                _run.restoring(lambda: undone.append("keys")), \
                _run.restoring(lambda: undone.append("screen")):
            with self.assertRaises(Quit):
                for event in run:
                    if isinstance(event, _events.Saved) and run.report.saved == 1:
                        # Three in a row, on the reader's thread as a terminal sends them.
                        signal.raise_signal(signal.SIGINT)
                        self.assertEqual(run._stop.stage, "soon")
                        signal.raise_signal(signal.SIGINT)
                        self.assertEqual(run._stop.stage, "now")
                        # Beside it, the target may say which files are finishing.
                        self.assertEqual(warn.call_args_list.count(mock.call(_run.AGAIN)), 1)
                        signal.raise_signal(signal.SIGINT)
        exit_now.assert_called_once_with(130)
        # Newest first, the way the `with` blocks would have unwound.
        self.assertEqual(undone, ["screen", "keys"])
        self.assertIn("Ctrl-C again to quit now", _run.AGAIN)
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        # The worker saw the second Ctrl-C: it ends on its own, keeping its parts.
        with self.assertRaises(errors.Stopped):
            run.wait()

    def test_async_for_and_await(self):
        async def main():
            events = [event async for event in self.fl.get("exs:example_user|reels")]
            report = await self.fl.get("exs:example_user|posts")
            return events, report

        events, report = asyncio.run(main())
        self.assertEqual(self.saved(events), ["r1"])
        self.assertEqual(report.saved, 2)

    def test_a_usage_mistake_raises_before_anything_runs(self):
        for call in (lambda: self.fl.get("zzz:example_user"),
                     lambda: self.fl.get("exs:example_user|stories"),
                     lambda: self.fl.get("exs:p/1|posts"),
                     lambda: self.fl.get("exs:example_user", limit=0),
                     lambda: self.fl.get("exs:example_user", when="yesterday")):
            with self.subTest(), self.assertRaises(errors.UsageError):
                call()
        self.assertEqual(self.requests, [])

    def test_leaving_the_with_stops_a_run_and_refuses_a_new_start(self):
        self.listing["example_user", "posts"] = [f"p{n}" for n in range(30)]
        with _library.open(self.home) as fl:
            self.enterContext(registered(self.source))
            going = fl.get("exs:example_user|posts")
            next(iter(going))
            later = fl.get("exs:example_user")
        self.assertTrue(going.report.stopped)
        with self.assertRaises(errors.UsageError):
            later.wait()

    def test_the_run_works_on_its_own_thread_with_the_callers_clock(self):
        from flnkit import clock as _time

        seen = []
        self.enterContext(registered(make_plugin("ewk", lambda ctx: seen.append(
            (threading.current_thread(), _time.wall())))))
        self.fl.get("ewk:example_user").wait()
        [(thread, wall)] = seen
        self.assertIsNot(thread, threading.current_thread())
        self.assertEqual(wall, self.fake.wall)


class EventsTest(LibraryCase):
    def test_saved_skipped_and_the_snapshot(self):
        run = self.fl.get("exs:example_user")
        first = list(run)
        saved = [event for event in first if isinstance(event, _events.Saved)]
        self.assertEqual(sorted(event.path.name for event in saved),
                         ["2026-03-01_p1.jpg", "2026-03-01_p2.jpg", "2026-03-01_r1.jpg"])
        self.assertTrue(all(event.path.is_file() and event.size == len(JPEG) for event in saved))
        self.assertEqual({event.date for event in saved}, {datetime.date(2026, 3, 1)})
        [row] = run.state.targets
        self.assertEqual((row.categories, row.phase, row.saved), (("posts", "reels"),
                                                                  _events.Phase.DONE, 3))
        second = list(self.fl.get("exs:example_user"))
        self.assertEqual(sum(isinstance(event, _events.Skipped) for event in second), 3)
        self.assertFalse(any(isinstance(event, _events.Saved) for event in second))

    def test_a_failed_file_is_an_event_and_a_line_of_the_report(self):
        self.listing["example_user", "posts"] = ["p1", "gone"]

        def serve(request):
            status = 404 if request.url.path.endswith("gone.jpg") else 200
            return _net.reply(status, content=JPEG)

        self.serve = lambda request: serve(request) if str(request.url).startswith(CDN) \
            else _net.reply(200)
        events = list(self.fl.get("exs:example_user|posts"))
        [failed] = [event for event in events if isinstance(event, _events.Failed)]
        self.assertEqual((failed.post, failed.path.name), ("gone", "2026-03-01_gone.jpg"))
        self.assertIsInstance(failed.error, errors.NotFound)

    def test_a_target_without_its_login_fails_as_an_event(self):
        self.enterContext(registered(example_source({("example_user", "posts"): ["p1"]}, code="exl",
                                   login=login_cookies("sid", required=True))))
        report = self.fl.get("exl:example_user").wait()
        [failed] = report.failed
        self.assertIsInstance(failed.error, errors.LoginError)
        self.assertEqual((failed.target.plugin, failed.post, failed.path),
                         ("exl", "", None))
        self.assertFalse(report.ok)

    def test_events_are_frozen(self):
        [event, *_] = [event for event in self.fl.get("exs:example_user")
                       if isinstance(event, _events.Saved)]
        with self.assertRaises(AttributeError):
            event.size = 0


class StoppedRunTest(LibraryCase):
    """E2: a run stopped after 3 saved files keeps what it saved in its `runs` row."""

    listing = {("example_user", "posts"): ["p1", "p2", "p3", "p4", "p5", "p6"]}

    def test_a_stopped_runs_row_counts_what_it_saved(self):
        run = self.fl.get("exs:example_user")
        saved = 0
        for event in run:
            if isinstance(event, _events.Saved):
                saved += 1
                if saved == 3:
                    run.stop()
        self.assertGreaterEqual(saved, 3)
        self.assertEqual(self.fl.status.saved, saved)


class SearchSkipTest(LibraryCase):
    """E19: a search's sync counts skipped files, never the targets it had seen."""

    def test_skipped_counts_files_only(self):
        found = ["exq:example_user", "exq:example_creator"]
        listing = {("example_handle", "posts"): ["p1", "p2", "p3"]}

        async def search(self, ctx, words):
            for text in found:
                yield Target(text)

        source = example_source(listing, code="exq", search=route("exq?{words}")(search))
        engine = Engine(home=self.home, sources={"exq": source})
        engine.download(["exq:example_handle|all"])
        engine.download(["exq?words|all"], sync=True, folder="{target}/{category}")
        # Two targets seen before, and a new one on top (a search lists newest first) whose
        # 3 files are saved already.
        found.insert(0, "exq:example_handle2")
        listing["example_handle2", "posts"] = ["p1", "p2", "p3"]
        [report] = engine.sync(["exq?words"]).targets
        self.assertEqual(report.skipped, 3)


class FollowTest(LibraryCase):
    def test_categories_are_kept_for_later_syncs(self):
        self.assertEqual(self.saved(self.fl.get("exs:example_user|reels")), ["r1"])
        self.listing["example_user", "posts"].insert(0, "p0")
        self.listing["example_user", "reels"].insert(0, "r0")
        self.assertEqual(self.saved(self.fl.get()), ["r0"])
        # Following again without categories keeps them; `/all` follows every one.
        self.assertEqual(self.saved(self.fl.get("exs:example_user")), [])
        self.assertEqual(self.saved(self.fl.get("exs:example_user|all")), ["p0", "p1", "p2"])
        self.assertEqual(self.saved(self.fl.get()), [])
        self.assertEqual(len(self.fl.files(target="exs:example_user")), 5)


class ReadsTest(LibraryCase):
    def test_files_by_target_kind_and_since(self):
        self.fl.get("exs:example_user").wait()
        self.fl.get("exs:p/9").wait()
        records = self.fl.files()
        self.assertEqual(sorted(record.post for record in records), ["9", "p1", "p2", "r1"])
        mine = self.fl.files(target="exs:example_user", kind="image", since="2026-03")
        self.assertEqual(sorted(record.post for record in mine), ["p1", "p2", "r1"])
        self.assertEqual(self.fl.files(since="2026-04"), [])
        self.assertEqual(self.fl.files(kind="video"), [])
        self.assertTrue(all(record.path.is_file() for record in records))
        with self.assertRaises(errors.UsageError):
            self.fl.files(kind="photo")

    def test_a_new_home_reads_empty_and_stays_unwritten(self):
        with _library.open(self.home / "new") as fl:
            self.assertEqual((list(fl.targets), fl.files()), ([], []))
            self.assertEqual(fl.get().wait(), _events.Report())
        self.assertFalse((self.home / "new" / "media").exists())

    def test_home_defaults_to_flnd_home_then_dot_flnd(self):
        from unittest import mock

        with mock.patch.dict("os.environ", {"FLND_HOME": "~/example-home"}):
            self.assertEqual(_library.Flnd().home, Path("~/example-home").expanduser())
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_library.open().home, Path(".flnd"))


class ScriptTest(LibraryCase):
    """`fl.plugin(code)`: a plugin's `Ctx` for a script, on the caller's loop."""

    API = "https://exc.example/api"

    def setUp(self):
        super().setUp()
        self.enterContext(registered(example_source({}, code="exc", login=login_cookies("sid"))))
        sign_in("exc", [{"name": "sid", "value": "v-one", "domain": ".exc.example",
                         "path": "/"}])

    def use(self, work, *, code="exc"):
        async def script():
            async with self.fl.plugin(code) as ctx:
                return await work(ctx)
        return drive(script())

    def test_it_fetches_with_the_plugins_cookies(self):
        async def work(ctx):
            return (await ctx.fetch(f"{self.API}/me")).status
        self.assertEqual(self.use(work), 200)
        self.assertIn("v-one", self.requests[-1].headers.get("cookie", ""))

    def test_retries_and_holds_work_as_in_a_run(self):
        answers = [_net.reply(503), _net.reply(200, content=b"ok")]
        self.routes[f"{self.API}/me"] = lambda request: answers.pop(0)

        async def work(ctx):
            return (await ctx.fetch(f"{self.API}/me")).status
        self.assertEqual(self.use(work), 200)
        self.assertEqual(len(self.requests), 2)
        # The retry waited on the fake clock, not the real one.
        self.assertGreater(self.now, 0)

    def test_state_and_cookies_are_written_back_on_exit_and_after_an_error(self):
        from flnd.engine import auth

        async def work(ctx):
            ctx.state["seen"] = 3
            raise ValueError("example")
        with mock.patch.object(auth, "write_back", wraps=auth.write_back) as back:
            with self.assertRaises(ValueError):
                self.use(work)
        self.assertEqual(back.call_args.args[0], "exc")

        async def read(ctx):
            return ctx.state.get("seen")
        self.assertEqual(self.use(read), 3)

    def test_a_missing_plugin_says_what_to_do(self):
        async def work(ctx):
            self.fail("the block must not run")
        with self.assertRaises(errors.UsageError):
            self.use(work, code="nope")

    def test_a_plugin_that_needs_a_login_it_lacks_raises_login_error(self):
        self.enterContext(registered(example_source({}, code="exr", login=login_cookies("sid", required=True))))

        async def work(ctx):
            self.fail("the block must not run")
        with self.assertRaises(errors.LoginError):
            self.use(work, code="exr")


class FolderListTest(LibraryCase):
    """`folder=` as a list or a mapping by category, and a search's need of one."""

    def media(self, code="exs"):
        root = self.home / "media" / code
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())

    def test_each_category_goes_where_its_entry_says_and_a_left_out_one_keeps_its_place(self):
        self.fl.get("exs:example_user|all", folder="posts:feed/{target}/{date}").wait()
        self.assertEqual(self.media(), [
            "example_user/reels/2026-03-01_r1.jpg",
            "feed/example_user/2026-03-01/2026-03-01_p1.jpg",
            "feed/example_user/2026-03-01/2026-03-01_p2.jpg"])

    def test_a_star_takes_every_category_not_named_and_a_mapping_is_the_same(self):
        self.fl.get("exs:example_user|all", folder={"posts": "p", "*": "other/{category}"}).wait()
        self.assertEqual(self.media(), [
            "other/reels/2026-03-01_r1.jpg", "p/2026-03-01_p1.jpg", "p/2026-03-01_p2.jpg"])

    def test_a_config_list_names_categories_and_a_wrong_one_is_refused(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.toml").write_text('[plugins.exs]\nfolder = "posts:a/{category}"\n',
                                               encoding="utf-8")
        self.fl.get("exs:example_user|all").wait()
        self.assertEqual(self.media(), [
            "example_user/a/posts/2026-03-01_p1.jpg", "example_user/a/posts/2026-03-01_p2.jpg",
            "example_user/reels/2026-03-01_r1.jpg"])
        (self.home / "config.toml").write_text('[plugins.exs]\nfolder = "posts:b,nope:c"\n',
                                               encoding="utf-8")
        with self.assertRaises(errors.UsageError):
            self.fl.get("exs:example_user")

    def test_a_wrong_folder_list_is_refused_before_a_request(self):
        for bad in ("posts:a,reels", "nope:a", "posts:a,posts:b", "posts:{nothere}"):
            with self.subTest(bad=bad), self.assertRaises(errors.UsageError):
                self.fl.get("exs:example_user", folder=bad)
        self.assertEqual(self.requests, [])


class SearchFolderTest(LibraryCase):
    """A search is refused without a folder that places every category, keeps the one it was
    given, and is refused another unless `again`."""

    def setUp(self):
        super().setUp()
        self.found = ["exq:example_user"]

        async def search(self_, ctx, words):
            for text in list(self.found):
                yield Target(text)

        self.enterContext(registered(example_source(self.listing, code="exq",
                                   search=route("exq?{words}")(search))))

    def media(self):
        root = self.home / "media" / "exq"
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())

    def test_a_search_without_a_folder_or_one_for_every_category_is_refused(self):
        for folder, message in (
            (None, "exq?words: a search needs --folder"),
            ("posts:{query}/p", "exq?words: a search needs --folder to place every category"),
        ):
            with self.subTest(folder=folder), self.assertRaises(errors.UsageError) as found:
                self.fl.get("exq?words", folder=folder)
            self.assertEqual(str(found.exception).split(" (")[0], message)
        self.assertEqual((self.requests, self.fl.targets.list()), ([], []))
        self.fl.get("exq?words|posts", folder="posts:{query}/p,*:{query}/o").wait()
        self.assertEqual(self.media(), ["words/p/2026-03-01_p1.jpg", "words/p/2026-03-01_p2.jpg"])

    def test_a_followed_search_keeps_its_folder_and_a_plain_sync_reuses_it(self):
        self.fl.get("exq?words", folder="{query}/{category}").wait()
        row = next(row for row in self.fl.targets if row.target.value == "words")
        self.assertTrue(row.follow)
        self.found.insert(0, "exq:example_creator")
        self.listing["example_creator", "posts"] = ["c1"]
        self.fl.get().wait()
        self.assertIn("words/posts/2026-03-01_c1.jpg", self.media())
        # It is not asked for a folder again, given or not.
        self.fl.get("exq?words").wait()
        self.fl.get("exq?words", folder="{query}/{category}").wait()

    def test_another_folder_is_refused_for_a_followed_search_unless_again(self):
        self.fl.get("exq?words", folder="one/{category}").wait()
        with self.assertRaisesRegex(errors.UsageError, "saved under --folder 'one/{category}'"):
            self.fl.get("exq?words", folder="two/{category}")
        with _library.open(self.home, again=True) as again:
            again.get("exq?words", folder="two/{category}").wait()
        self.found.insert(0, "exq:example_creator")
        self.listing["example_creator", "posts"] = ["c1"]
        self.fl.get().wait()
        self.assertIn("two/posts/2026-03-01_c1.jpg", self.media())


class HomeLockTest(LibraryCase):
    """One run per home: `<home>/.lock`, and the commands that never take it."""

    def locked(self):
        return _files.lock(self.home / ".lock")

    def test_the_commands_that_write_rows_a_run_lands_are_refused_on_a_locked_home(self):
        self.fl.get("exs:example_user").wait()
        calls = (lambda: self.fl.get("exs:example_user").wait(), lambda: self.fl.sync().wait(),
                 lambda: self.fl.targets.clean("exs:example_user"),
                 lambda: self.fl.targets.clean("exs:example_user", media=True),
                 lambda: self.fl.check(fix=True))
        with self.locked() as free:
            self.assertTrue(free)
            for call in calls:
                with self.subTest(), self.assertRaises(errors.UsageError) as caught:
                    call()
                self.assertEqual(str(caught.exception), f"another flnd run is using {self.home}")
                self.assertEqual(caught.exception.hint,
                                 "wait for it to finish, or use another --home")
        self.assertEqual(len(list(self.fl.targets)), 1)
        self.assertTrue(any((self.home / "media").rglob("*.jpg")))

    def test_a_dry_get_or_sync_works_on_a_locked_home_and_leaves_the_lock(self):
        self.fl.get("exs:example_user").wait()
        with self.locked() as free:
            self.assertTrue(free)
            self.assertEqual(len(list(self.fl.get("exs:example_user", dry=True))), 3)
            self.fl.sync(dry=True).wait()
            with self.locked() as second:
                self.assertFalse(second, "the lock is still its holder's")

    def test_follow_retry_and_the_reads_never_take_the_lock(self):
        self.fl.get("exs:example_user").wait()
        with self.locked() as free:
            self.assertTrue(free)
            self.fl.targets.unfollow("exs:example_user")
            self.fl.targets.follow("exs:example_user")
            self.fl.targets.retry("exs:example_user")
            self.assertEqual(len(list(self.fl.targets)), 1)
            self.assertTrue(self.fl.check().ok)
            self.assertIsNotNone(self.fl.status)
            self.assertEqual(list(self.fl.auth), [])
            self.assertIn("exs", self.fl.config.where("defaults.when") + "exs")

    def test_the_lock_is_free_after_a_command_and_after_an_error(self):
        self.fl.get("exs:example_user").wait()
        with self.locked() as free:
            self.assertTrue(free)
        with self.assertRaises(errors.UsageError):
            self.fl.targets.clean("exs:nobody:else")
        with self.locked() as free:
            self.assertTrue(free)
        self.fl.targets.clean("exs:example_user")
        self.fl.check(fix=True)
        with self.locked() as free:
            self.assertTrue(free)
        self.assertEqual((self.home / ".lock").read_bytes(), b"")

    def test_a_follow_write_during_a_commit_is_waited_for_and_the_run_goes_on(self):
        self.fl.get("exs:example_user").wait()
        self.listing["example_user", "posts"].insert(0, "p0")
        holder = sqlite3.connect(self.home / _store.DB, isolation_level=None,
                                 check_same_thread=False)
        release = threading.Timer(0.3, lambda: holder.execute("COMMIT"))
        follow = threading.Thread(target=lambda: self.fl.targets.follow("exs:example_creator"))
        serve = self.serve

        def held(request):
            # The first file's answer: another process starts writing `flnd.db`, as a
            # `target follow` does, and the run's commit meets its lock.
            if not release.is_alive() and str(request.url).startswith(CDN):
                holder.execute("BEGIN IMMEDIATE")
                release.start()
                follow.start()
            return serve(request)

        self.serve = held
        requeued = []
        busy = jobs_module.Scheduler._busy

        async def counted(scheduler, job, error, tries):
            requeued.append(error)
            return await busy(scheduler, job, error, tries)

        try:
            with mock.patch.object(jobs_module.Scheduler, "_busy", counted):
                report = self.fl.get("exs:example_user").wait()
        finally:
            release.join()
            follow.join(10)
            holder.close()
        self.assertEqual((report.saved, report.ok), (1, True))
        self.assertTrue(requeued, "the commit met the lock and was queued again")
        self.assertEqual(sorted(row.target.value for row in self.fl.targets),
                         ["example_creator", "example_user"])

if __name__ == "__main__":
    unittest.main()
