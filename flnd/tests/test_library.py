"""The library (`flnd.open`, `Flnd`, `Run`, events), end to end over fakes. No network, no browser.

The gate of the library is that every command of today's CLI can be done through it, so
`CliCommandsTest` has one test per command, each doing what the command does with the library
alone. The other classes test a `Run` (lazy start, the warning when it is never started, stop,
async), the events and the report, the reads, and `when`.

Every test runs on `EngineCase`: the fake network answers from `self.routes`, the clock is fake, and the
home is a temporary folder.
"""

import asyncio
import datetime
import gc
import threading
import unittest
import warnings
from pathlib import Path
from unittest import mock

from fakes import CDN, Engine, EngineCase, login_cookies, make_plugin, noise

import flnd
from flnd.engine import jobs as jobs_module
from flnd.engine.events import encode
from flnd.plugin import File, Plugin, Post, Target, route
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


class LibraryCase(EngineCase):
    """A home with the example source added, and every `<CDN>/<id>.jpg` a small JPEG."""

    listing = {("example_user", "posts"): ["p1", "p2"], ("example_user", "reels"): ["r1"]}

    def setUp(self):
        super().setUp()
        self.listing = {key: list(ids) for key, ids in type(self).listing.items()}
        self.source = example_source(self.listing)
        self.fl = self.enterContext(flnd.open(self.home))
        self.fl.add(self.source)

    def serve(self, request):
        if str(request.url).startswith(CDN):
            self.requests.append(request)
            return _net.reply(200, content=JPEG)
        return super().serve(request)

    def saved(self, events):
        return sorted(event.post for event in events if isinstance(event, flnd.Saved))


class GetTest(LibraryCase):
    """`get`, and each group of the library (PLAN.md 8.3), as the commands use them."""

    def test_get_runs_and_follows_the_target(self):
        report = self.fl.get("exs:example_user").wait()
        self.assertEqual((report.saved, report.ok), (3, True))
        self.assertEqual(report.bytes, 3 * len(JPEG))
        [tracked] = self.fl.targets
        self.assertEqual(tracked.target, flnd.Target("exs:example_user", kind="user"))
        self.assertEqual((tracked.files, tracked.follow), (3, True))
        self.assertEqual(tracked.categories, ("posts", "reels"))
        self.assertEqual(tracked.complete, {"posts": tracked.complete["posts"],
                                            "reels": tracked.complete["reels"]})

    def test_get_with_categories_when_and_limit(self):
        run = self.fl.get("exs:example_user/reels", when="2026", limit=5)
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

    def test_a_single_post_is_never_followed(self):
        self.fl.get("exs:p/9").wait()
        [tracked] = self.fl.targets
        self.assertFalse(tracked.follow)
        with self.assertRaises(flnd.UsageError):
            self.fl.targets.follow("exs:p/9")

    def test_follow_unfollow_and_retry(self):
        [tracked] = self.fl.targets.follow("exs:example_user")
        self.assertTrue(tracked.follow)
        self.fl.targets.unfollow("exs:example_user")
        self.assertFalse(list(self.fl.targets)[0].follow)
        self.assertEqual(self.fl.get().wait().saved, 0)
        self.fl.get("exs:example_user").wait()
        self.assertEqual(len(self.fl.targets.retry("exs:example_user")), 1)
        with self.assertRaises(flnd.UsageError):
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

    def test_auth_login_logout_and_default(self):
        self.fl.add(example_source({}, code="exc", login=login_cookies("sid")))
        cookies = Path(self.home.parent / "cookies.txt")
        cookies.write_text("# Netscape HTTP Cookie File\n"
                           ".exc.example\tTRUE\t/\tTRUE\t1825861327\tsid\texample-value\n",
                           encoding="utf-8")
        first = self.fl.auth.login("exc", method="file", path=str(cookies), name="one")
        second = self.fl.auth.login("exc", path=str(cookies), name="two")
        self.assertEqual((first.origin, first.is_default, second.is_default), ("file", True, False))
        self.assertEqual([item.name for item in self.fl.auth], ["one", "two"])
        self.assertTrue(self.fl.auth.default("exc", "two").is_default)
        self.assertEqual(self.fl.auth.logout("exc").name, "two")
        self.assertEqual(self.fl.auth.logout("exc", name="one").name, "one")
        for call in (lambda: self.fl.auth.logout("exc"),
                     lambda: self.fl.auth.login("exc", method="nope"),
                     lambda: self.fl.auth.login("exs")):
            with self.subTest(), self.assertRaises(flnd.UsageError):
                call()

    def test_plugins_lists_built_in_and_added(self):
        rows = {row.code: row for row in self.fl.plugins}
        self.assertEqual((rows["ins"].origin, rows["exs"].origin), ("built-in", "script"))
        with self.assertRaises(flnd.UsageError):
            self.fl.plugins.uninstall("ins")

    def test_config_reads_sets_and_unsets(self):
        self.assertEqual((self.fl.config["when"], self.fl.config.where("when")), ("", "default"))
        self.fl.config.set("defaults.when", "30d")
        self.assertEqual((self.fl.config["defaults.when"], self.fl.config.where("when")),
                         ("30d", "home"))
        self.fl.config.unset("when")
        self.assertEqual(self.fl.config["when"], "")
        with self.assertRaises(flnd.UsageError):
            self.fl.config.set("nope", 1)


class RunTest(LibraryCase):
    def test_nothing_happens_until_the_run_is_read(self):
        run = self.fl.get("exs:example_user")
        self.assertEqual((self.requests, run.report), ([], flnd.Report()))
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
            if isinstance(event, flnd.Saved):
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
        run = self.fl.get("exs:example_user/posts")
        landed, real = threading.Event(), run._emit

        def emit(event):
            done = real(event)
            if run.report.saved == 6:
                landed.set()
            return done

        run._emit = emit
        events = iter(run)
        first = next(event for event in events if isinstance(event, flnd.Saved))
        self.assertTrue(landed.wait(30), "the other files waited for the reader")
        self.assertEqual(len([first, *(e for e in events if isinstance(e, flnd.Saved))]), 6)

    def test_stop_ends_the_run_without_an_error(self):
        # One place, so most files are still queued when the first lands: a stop drops those and
        # finishes only what is downloading. At the default of 32 places all 30 would be in flight.
        self.enterContext(mock.patch.object(jobs_module, "PLACES", 1))
        self.listing["example_user", "posts"] = [f"p{n}" for n in range(30)]
        run = self.fl.get("exs:example_user/posts")
        for event in run:
            if isinstance(event, flnd.Saved):
                run.stop()
        report = run.report
        self.assertTrue(report.stopped)
        self.assertLess(report.saved, 30)

    def test_ctrl_c_while_reading_stops_the_run_and_raises_stopped(self):
        import signal

        # One place, as above: at 32 places every file could be in flight before the first lands.
        self.enterContext(mock.patch.object(jobs_module, "PLACES", 1))
        self.listing["example_user", "posts"] = [f"p{n}" for n in range(30)]
        run = self.fl.get("exs:example_user/posts")
        with self.assertRaises(flnd.Stopped):
            for event in run:
                if isinstance(event, flnd.Saved) and run.report.saved == 1:
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
        run = self.fl.get("exs:example_user/posts")
        with mock.patch.object(_run.os, "_exit", side_effect=Quit) as exit_now, \
                _run.restoring(lambda: undone.append("keys")), \
                _run.restoring(lambda: undone.append("screen")):
            with self.assertRaises(Quit):
                for event in run:
                    if isinstance(event, flnd.Saved) and run.report.saved == 1:
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
        with self.assertRaises(flnd.Stopped):
            run.wait()

    def test_async_for_and_await(self):
        async def main():
            events = [event async for event in self.fl.get("exs:example_user/reels")]
            report = await self.fl.get("exs:example_user/posts")
            return events, report

        events, report = asyncio.run(main())
        self.assertEqual(self.saved(events), ["r1"])
        self.assertEqual(report.saved, 2)

    def test_a_usage_mistake_raises_before_anything_runs(self):
        for call in (lambda: self.fl.get("zzz:example_user"),
                     lambda: self.fl.get("exs:example_user/stories"),
                     lambda: self.fl.get("exs:p/1/posts"),
                     lambda: self.fl.get("exs:example_user", limit=0),
                     lambda: self.fl.get("exs:example_user", when="yesterday")):
            with self.subTest(), self.assertRaises(flnd.UsageError):
                call()
        self.assertEqual(self.requests, [])

    def test_leaving_the_with_stops_a_run_and_refuses_a_new_start(self):
        self.listing["example_user", "posts"] = [f"p{n}" for n in range(30)]
        with flnd.open(self.home) as fl:
            fl.add(self.source)
            going = fl.get("exs:example_user/posts")
            next(iter(going))
            later = fl.get("exs:example_user")
        self.assertTrue(going.report.stopped)
        with self.assertRaises(flnd.UsageError):
            later.wait()

    def test_the_run_works_on_its_own_thread_with_the_callers_clock(self):
        from flnkit import clock as _time

        seen = []
        self.fl.add(make_plugin("ewk", lambda ctx: seen.append(
            (threading.current_thread(), _time.wall()))))
        self.fl.get("ewk:example_user").wait()
        [(thread, wall)] = seen
        self.assertIsNot(thread, threading.current_thread())
        self.assertEqual(wall, self.fake.wall)


class EventsTest(LibraryCase):
    def test_saved_skipped_and_the_snapshot(self):
        run = self.fl.get("exs:example_user")
        first = list(run)
        saved = [event for event in first if isinstance(event, flnd.Saved)]
        self.assertEqual(sorted(event.path.name for event in saved),
                         ["2026-03-01_p1.jpg", "2026-03-01_p2.jpg", "2026-03-01_r1.jpg"])
        self.assertTrue(all(event.path.is_file() and event.size == len(JPEG) for event in saved))
        self.assertEqual({event.date for event in saved}, {datetime.date(2026, 3, 1)})
        [row] = run.state.targets
        self.assertEqual((row.categories, row.phase, row.saved), (("posts", "reels"),
                                                                  flnd.Phase.DONE, 3))
        second = list(self.fl.get("exs:example_user"))
        self.assertEqual(sum(isinstance(event, flnd.Skipped) for event in second), 3)
        self.assertFalse(any(isinstance(event, flnd.Saved) for event in second))

    def test_a_failed_file_is_an_event_and_a_line_of_the_report(self):
        self.listing["example_user", "posts"] = ["p1", "gone"]

        def serve(request):
            status = 404 if request.url.path.endswith("gone.jpg") else 200
            return _net.reply(status, content=JPEG)

        self.serve = lambda request: serve(request) if str(request.url).startswith(CDN) \
            else _net.reply(200)
        events = list(self.fl.get("exs:example_user/posts"))
        [failed] = [event for event in events if isinstance(event, flnd.Failed)]
        self.assertEqual((failed.post, failed.path.name), ("gone", "2026-03-01_gone.jpg"))
        self.assertIsInstance(failed.error, flnd.NotFound)

    def test_a_target_without_its_login_fails_as_an_event(self):
        self.fl.add(example_source({("example_user", "posts"): ["p1"]}, code="exl",
                                   login=login_cookies("sid", required=True)))
        report = self.fl.get("exl:example_user").wait()
        [failed] = report.failed
        self.assertIsInstance(failed.error, flnd.LoginError)
        self.assertEqual((failed.target.plugin, failed.post, failed.path),
                         ("exl", "", None))
        self.assertFalse(report.ok)

    def test_events_encode_to_json(self):
        events = list(self.fl.get("exs:example_user/reels"))
        lines = [encode(event) for event in events]
        self.assertTrue(any(line.startswith(b'{"type":"Saved"') for line in lines))
        self.assertIn(b'"path":"', lines[-1])
        login = flnd.Failed(flnd.Target("exs:example_user", kind="user"),
                            flnd.LoginError("not logged in", hint="log in"))
        self.assertIn(b'"error":{"class":"LoginError","message":"not logged in","hint":"log in"}',
                      encode(login))

    def test_events_are_frozen(self):
        [event, *_] = [event for event in self.fl.get("exs:example_user")
                       if isinstance(event, flnd.Saved)]
        with self.assertRaises(AttributeError):
            event.size = 0


class StoppedRunTest(LibraryCase):
    """E2: a run stopped after 3 saved files keeps what it saved in its `runs` row."""

    listing = {("example_user", "posts"): ["p1", "p2", "p3", "p4", "p5", "p6"]}

    def test_a_stopped_runs_row_counts_what_it_saved(self):
        run = self.fl.get("exs:example_user")
        saved = 0
        for event in run:
            if isinstance(event, flnd.Saved):
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
        engine.download(["exq:example_handle/all"])
        engine.download(["exq?words/all"], sync=True)
        # Two targets seen before, and a new one whose 3 files are saved already.
        found.append("exq:example_handle2")
        listing["example_handle2", "posts"] = ["p1", "p2", "p3"]
        [report] = engine.sync(["exq?words"]).targets
        self.assertEqual(report.skipped, 3)


class FollowTest(LibraryCase):
    def test_categories_are_kept_for_later_syncs(self):
        self.assertEqual(self.saved(self.fl.get("exs:example_user/reels")), ["r1"])
        self.listing["example_user", "posts"].insert(0, "p0")
        self.listing["example_user", "reels"].insert(0, "r0")
        self.assertEqual(self.saved(self.fl.get()), ["r0"])
        # Following again without categories keeps them; `/all` follows every one.
        self.assertEqual(self.saved(self.fl.get("exs:example_user")), [])
        self.assertEqual(self.saved(self.fl.get("exs:example_user/all")), ["p0", "p1", "p2"])
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
        with self.assertRaises(flnd.UsageError):
            self.fl.files(kind="photo")

    def test_a_new_home_reads_empty_and_stays_unwritten(self):
        with flnd.open(self.home / "new") as fl:
            self.assertEqual((list(fl.targets), fl.files()), ([], []))
            self.assertEqual(fl.get().wait(), flnd.Report())
        self.assertFalse((self.home / "new" / "media").exists())

    def test_add_refuses_a_taken_code_or_a_non_source(self):
        with self.assertRaises(flnd.UsageError):
            self.fl.add(self.source)
        with self.assertRaises(flnd.UsageError):
            self.fl.add(object)

    def test_home_defaults_to_flnd_home_then_dot_flnd(self):
        from unittest import mock

        with mock.patch.dict("os.environ", {"FLND_HOME": "~/example-home"}):
            self.assertEqual(flnd.Flnd().home, Path("~/example-home").expanduser())
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(flnd.open().home, Path(".flnd"))


if __name__ == "__main__":
    unittest.main()
