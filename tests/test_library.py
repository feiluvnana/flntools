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

from test_engine import CDN, EngineCase, make_source, noise

import flnd
from flnd import _net
from flnd import site as site_module
from flnd._events import encode
from flnd._library import _when
from flnd.source import File, Post, Source, target
from flnd.source import cookies as login_cookies

JPEG = noise("JPEG")


def example_source(listing, *, code="exs", **declared):
    """A source with two categories and single posts, listing what `listing` holds.

    Args:
        listing: `{(user, category): [post ids]}`; each post has one file, `<CDN>/<id>.jpg`.
        code: the source's code; its domain is `<code>.example`.
        **declared: the class's other fields, such as `login`.
    """
    def posts_of(user, category):
        for post_id in listing.get((user, category), []):
            yield Post(post_id, [File(f"{CDN}/{post_id}.jpg")], date=datetime.date(2026, 3, 1))

    def post(self, site, id):
        yield Post(id, [File(f"{CDN}/{id}.jpg")], date=datetime.date(2026, 3, 1))

    def user(self, site, user):
        return user

    def posts(self, site, user):
        yield from posts_of(user, "posts")

    def reels(self, site, user):
        yield from posts_of(user, "reels")

    profile = target(f"{code}:{{user}}")(user)
    profile.category(posts)
    profile.category(reels)
    attributes = {"code": code, "name": "Example", "domains": (f"{code}.example",), "api": 1,
                  "folder": "{category}", "key": "{post}", "filename": "{date}_{post}{index}",
                  "post": target(f"{code}:p/{{id}}")(post), "user": profile, "posts": posts,
                  "reels": reels, **declared}
    return type("Example", (Source,), attributes)


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


class CliCommandsTest(LibraryCase):
    """Each command of today's CLI, done through the library alone."""

    def test_download(self):
        # flnd download exs:example_user --from all
        report = self.fl.download("exs:example_user").wait()
        self.assertEqual((report.saved, report.ok), (3, True))
        self.assertEqual(report.bytes, 3 * len(JPEG))

    def test_download_from(self):
        # flnd download exs:example_user --from reels --time 2026-01-01,2026-12-31 --limit 5
        run = self.fl.download("exs:example_user", only=["reels"], when="2026", limit=5)
        self.assertEqual(self.saved(run), ["r1"])

    def test_download_dry(self):
        # flnd download exs:example_user --dry
        planned = list(self.fl.download("exs:example_user", dry=True))
        self.assertEqual(sorted((event.post, event.state) for event in planned),
                         [("p1", "new"), ("p2", "new"), ("r1", "new")])
        self.assertFalse((self.home / "media").exists())

    def test_download_sync_is_follow(self):
        # flnd download exs:example_user --sync
        self.assertEqual(self.fl.follow("exs:example_user").wait().saved, 3)
        [tracked] = self.fl.targets()
        self.assertTrue(tracked.follow)

    def test_sync(self):
        # flnd sync
        self.fl.follow("exs:example_user").wait()
        self.listing["example_user", "posts"].insert(0, "p0")
        self.assertEqual(self.saved(self.fl.sync()), ["p0"])

    def test_sync_named(self):
        # flnd sync exs:example_user
        self.fl.follow("exs:example_user").wait()
        self.assertEqual(self.fl.sync("exs:example_user").wait().saved, 0)

    def test_target_ls(self):
        # flnd target ls
        self.fl.download("exs:example_user").wait()
        [tracked] = self.fl.targets(source="exs")
        self.assertEqual(tracked.target, flnd.Target("exs:example_user", kind="user"))
        self.assertEqual((tracked.files, tracked.follow), (3, False))
        self.assertEqual(tracked.categories, {"posts": "complete", "reels": "complete"})

    def test_target_sync_on(self):
        # flnd target sync-on exs:example_user
        self.fl.download("exs:example_user").wait()
        self.fl.follow("exs:example_user").wait()
        self.assertTrue(self.fl.targets()[0].follow)

    def test_target_sync_off_and_del(self):
        # flnd target sync-off exs:example_user; flnd target del exs:example_user
        self.fl.follow("exs:example_user").wait()
        self.assertEqual(self.fl.unfollow("exs:example_user"),
                         [flnd.Target("exs:example_user", kind="user")])
        self.assertFalse(self.fl.targets()[0].follow)
        self.listing["example_user", "posts"].insert(0, "p0")
        self.assertEqual(self.fl.sync().wait().saved, 0)
        # Its files and history stay.
        self.assertEqual(len(self.fl.files(target="exs:example_user")), 3)

    def test_auth_login_ls_and_logout(self):
        # flnd auth login (from a cookie file); flnd auth ls; flnd auth logout exs
        self.fl.add(example_source({}, code="exc", login=login_cookies("sid")))
        cookies = Path(self.home.parent / "cookies.txt")
        cookies.write_text("# Netscape HTTP Cookie File\n"
                           ".exc.example\tTRUE\t/\tTRUE\t1825861327\tsid\texample-value\n",
                           encoding="utf-8")
        login = self.fl.login("exc", str(cookies))
        self.assertEqual((login.source, login.state, login.origin),
                         ("exc", "logged in", "cookies.txt"))
        self.assertIn(login, self.fl.logins)
        self.assertTrue(self.fl.logout("exc"))
        self.assertFalse(self.fl.logout("exc"))
        state = {item.source: item.state for item in self.fl.logins}
        self.assertEqual(state["exc"], "not logged in")


class RunTest(LibraryCase):
    def test_nothing_happens_until_the_run_is_read(self):
        run = self.fl.download("exs:example_user")
        self.assertEqual((self.requests, run.report), ([], flnd.Report()))
        self.assertIn("not started", repr(run))
        run.wait()
        self.assertEqual(len(self.requests), 3)

    def test_an_unstarted_run_warns_when_dropped(self):
        run = self.fl.download("exs:example_user")
        with self.assertWarns(ResourceWarning) as caught:
            del run
            gc.collect()
        self.assertIn("never started", str(caught.warning))

    def test_a_started_or_stopped_run_does_not_warn(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            self.fl.download("exs:example_user").wait()
            unstarted = self.fl.download("exs:example_user")
            unstarted.stop()
            self.assertTrue(unstarted.wait().stopped)
            del unstarted
            gc.collect()

    def test_breaking_out_pauses_and_the_report_holds_the_counts_so_far(self):
        run = self.fl.download("exs:example_user")
        for event in run:
            if isinstance(event, flnd.Saved):
                break
        self.assertGreaterEqual(run.report.saved, 1)
        self.assertEqual(run.wait().saved, 3)
        self.assertEqual(list(run), [])

    def test_stop_ends_the_run_without_an_error(self):
        # One place, so most files are still queued when the first lands: a stop drops those and
        # finishes only what is downloading. At the default of 32 places all 30 would be in flight.
        self.enterContext(mock.patch.object(site_module, "MEDIA_MOST", 1))
        self.listing["example_user", "posts"] = [f"p{n}" for n in range(30)]
        run = self.fl.download("exs:example_user", only="posts")
        for event in run:
            if isinstance(event, flnd.Saved):
                run.stop()
        report = run.report
        self.assertTrue(report.stopped)
        self.assertLess(report.saved, 30)

    def test_ctrl_c_while_reading_stops_the_run_and_raises_stopped(self):
        import signal

        # One place, as above: at 32 places every file could be in flight before the first lands.
        self.enterContext(mock.patch.object(site_module, "MEDIA_MOST", 1))
        self.listing["example_user", "posts"] = [f"p{n}" for n in range(30)]
        run = self.fl.download("exs:example_user", only="posts")
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

        from flnd import _run
        from flnd._ui import ui

        class Quit(BaseException):
            """What the patched `os._exit` raises, so the test sees the exit instead of dying."""

        undone = []
        self.enterContext(mock.patch.object(site_module, "MEDIA_MOST", 1))
        self.listing["example_user", "posts"] = [f"p{n}" for n in range(30)]
        warn = self.enterContext(mock.patch.object(ui, "warn"))
        run = self.fl.download("exs:example_user", only="posts")
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
                        warn.assert_called_once_with(_run.AGAIN)
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
            events = [event async for event in self.fl.download("exs:example_user", only="reels")]
            report = await self.fl.download("exs:example_user", only="posts")
            return events, report

        events, report = asyncio.run(main())
        self.assertEqual(self.saved(events), ["r1"])
        self.assertEqual(report.saved, 2)

    def test_a_usage_mistake_raises_before_anything_runs(self):
        for call in (lambda: self.fl.download("zzz:example_user"),
                     lambda: self.fl.download("exs:example_user", only=["stories"]),
                     lambda: self.fl.download("exs:p/1", only=["posts"]),
                     lambda: self.fl.download("exs:example_user", limit=0),
                     lambda: self.fl.download("exs:example_user", when="yesterday"),
                     lambda: self.fl.download([])):
            with self.subTest(), self.assertRaises(flnd.UsageError):
                call()
        self.assertEqual(self.requests, [])

    def test_leaving_the_with_stops_a_run_and_refuses_a_new_start(self):
        self.listing["example_user", "posts"] = [f"p{n}" for n in range(30)]
        with flnd.open(self.home) as fl:
            fl.add(self.source)
            going = fl.download("exs:example_user", only="posts")
            next(iter(going))
            later = fl.download("exs:example_user")
        self.assertTrue(going.report.stopped)
        with self.assertRaises(flnd.UsageError):
            later.wait()

    def test_the_run_works_on_its_own_thread_with_the_callers_clock(self):
        from flnd import _time

        seen = []
        self.fl.add(make_source("ewk", lambda site: seen.append(
            (threading.current_thread(), _time.wall()))))
        self.fl.download("ewk:example_user").wait()
        [(thread, wall)] = seen
        self.assertIsNot(thread, threading.current_thread())
        self.assertEqual(wall, self.fake.wall)


class EventsTest(LibraryCase):
    def test_saved_skipped_and_progress(self):
        first = list(self.fl.download("exs:example_user"))
        saved = [event for event in first if isinstance(event, flnd.Saved)]
        self.assertEqual(sorted(event.path.name for event in saved),
                         ["2026-03-01_p1.jpg", "2026-03-01_p2.jpg", "2026-03-01_r1.jpg"])
        self.assertTrue(all(event.path.is_file() and event.size == len(JPEG) for event in saved))
        self.assertEqual({event.date for event in saved}, {datetime.date(2026, 3, 1)})
        progress = [event for event in first if isinstance(event, flnd.Progress)]
        self.assertEqual({event.category for event in progress}, {"posts", "reels"})
        self.assertTrue(any(event.listed for event in progress))
        second = list(self.fl.download("exs:example_user"))
        self.assertEqual(sum(isinstance(event, flnd.Skipped) for event in second), 3)
        self.assertFalse(any(isinstance(event, flnd.Saved) for event in second))

    def test_a_failed_file_is_an_event_and_a_line_of_the_report(self):
        self.listing["example_user", "posts"] = ["p1", "gone"]

        def serve(request):
            status = 404 if request.url.path.endswith("gone.jpg") else 200
            return _net.reply(status, content=JPEG)

        self.serve = lambda request: serve(request) if str(request.url).startswith(CDN) \
            else _net.reply(200)
        events = list(self.fl.download("exs:example_user", only="posts"))
        [failed] = [event for event in events if isinstance(event, flnd.Failed)]
        self.assertEqual((failed.post, failed.path.name), ("gone", "2026-03-01_gone.jpg"))
        self.assertIsInstance(failed.error, flnd.NotFound)

    def test_a_target_without_its_login_fails_as_an_event(self):
        self.fl.add(example_source({("example_user", "posts"): ["p1"]}, code="exl",
                                   login=login_cookies("sid", required=True)))
        report = self.fl.download("exl:example_user").wait()
        [failed] = report.failed
        self.assertIsInstance(failed.error, flnd.LoginError)
        self.assertEqual((failed.target.source, failed.post, failed.path),
                         ("exl", "", None))
        self.assertFalse(report.ok)

    def test_events_encode_to_json(self):
        events = list(self.fl.download("exs:example_user", only="reels"))
        lines = [encode(event) for event in events]
        self.assertTrue(any(line.startswith(b'{"type":"Saved"') for line in lines))
        self.assertIn(b'"path":"', lines[-1])
        login = flnd.Failed(flnd.Target("exs:example_user", kind="user"),
                            flnd.LoginError("not logged in", hint="log in"))
        self.assertIn(b'"error":{"class":"LoginError","message":"not logged in","hint":"log in"}',
                      encode(login))

    def test_events_are_frozen(self):
        [event, *_] = [event for event in self.fl.download("exs:example_user")
                       if isinstance(event, flnd.Saved)]
        with self.assertRaises(AttributeError):
            event.size = 0


class FollowTest(LibraryCase):
    def test_only_is_kept_for_later_syncs(self):
        self.assertEqual(self.saved(self.fl.follow("exs:example_user", only=["reels"])), ["r1"])
        self.listing["example_user", "posts"].insert(0, "p0")
        self.listing["example_user", "reels"].insert(0, "r0")
        self.assertEqual(self.saved(self.fl.sync()), ["r0"])
        # Following again without only follows every category.
        self.fl.follow("exs:example_user").wait()
        self.assertEqual(self.saved(self.fl.sync()), [])
        self.assertEqual(len(self.fl.files(target="exs:example_user")), 5)

    def test_download_never_follows(self):
        self.fl.download("exs:example_user").wait()
        self.assertFalse(self.fl.targets()[0].follow)


class ReadsTest(LibraryCase):
    def test_files_by_target_kind_and_since(self):
        self.fl.download("exs:example_user").wait()
        self.fl.download("exs:p/9").wait()
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
            self.assertEqual((fl.targets(), fl.files()), ([], []))
            self.assertEqual(fl.sync().wait(), flnd.Report())
        self.assertFalse((self.home / "new" / "media").exists())

    def test_add_refuses_a_taken_code_or_a_non_source(self):
        with self.assertRaises(flnd.UsageError):
            self.fl.add(self.source)
        with self.assertRaises(flnd.UsageError):
            self.fl.add(object)

    def test_site_gives_a_sources_site(self):
        with self.fl.site("exs") as site:
            self.assertEqual(str(site.target), "exs")
            self.assertEqual(site.fetch("https://exs.example/x").text, "/x")

    def test_home_defaults_to_flnd_home_then_dot_flnd(self):
        from unittest import mock

        with mock.patch.dict("os.environ", {"FLND_HOME": "~/example-home"}):
            self.assertEqual(flnd.Flnd().home, Path("~/example-home").expanduser())
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(flnd.open().home, Path(".flnd"))


class WhenTest(unittest.TestCase):
    def test_periods_ranges_and_spans(self):
        day = datetime.date
        self.assertEqual(_when(None), flnd.source.Range())
        self.assertEqual(_when("2026"), flnd.source.Range(day(2026, 1, 1), day(2026, 12, 31)))
        self.assertEqual(_when("2024-02"), flnd.source.Range(day(2024, 2, 1), day(2024, 2, 29)))
        self.assertEqual(_when("2026-12"), flnd.source.Range(day(2026, 12, 1), day(2026, 12, 31)))
        self.assertEqual(_when("2026-06-15"),
                         flnd.source.Range(day(2026, 6, 15), day(2026, 6, 15)))
        self.assertEqual(_when("2026-01..2026-03"),
                         flnd.source.Range(day(2026, 1, 1), day(2026, 3, 31)))
        self.assertEqual(_when("2026-01.."), flnd.source.Range(day(2026, 1, 1), None))
        self.assertEqual(_when("..2026-03"), flnd.source.Range(None, day(2026, 3, 31)))

    def test_a_span_counts_back_from_today_on_the_clock(self):
        from flnd import _time

        with _time.use(_time.Fake()):
            today = datetime.date.fromtimestamp(_time.wall())
            self.assertEqual(_when("30d").since, today - datetime.timedelta(days=30))
            self.assertEqual(_when("1y").since, today - datetime.timedelta(days=365))

    def test_bad_values(self):
        for text in ("yesterday", "2026-13", "2026-02-30", "2026-03..2026-01", "30", "d30"):
            with self.subTest(text=text), self.assertRaises(flnd.UsageError):
                _when(text)


if __name__ == "__main__":
    unittest.main()
