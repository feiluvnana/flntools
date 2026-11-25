"""The run (src/engine/run.py) and the engine on it, on `flnkit.net.fake`, with test plugins.

The event channel and `END`, then the engine's runs, plugin instances, HTTP, clients and logins
through its run (what `download` and `sync` await), then its commands: `download`, `sync`,
`login`, `logout` and `Report`. No network, no browser; the screen is a fake one.
"""

import asyncio
import contextlib
import os
import signal
import threading
import time
import unittest
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

import fakes
from fakes import (
    CDN,
    Engine,
    EngineCase,
    login_cookies,
    make_plugin,
    noise,
    own_login,
    sign_in,
    vaulted,
)

from flnd.engine import crawl as job
from flnd.engine import jobs
from flnd.engine import run as _run
from flnd.engine import store as _store
from flnd.engine._engine import Results, _Runner
from flnd.engine.crawl import Options, TargetReport
from flnd.engine.events import Failed, Skipped
from flnd.errors import Blocked, Error, LoginError, SiteError, UsageError
from flnd.plugin import Browser, File, Plugin, Post, Request, Target, route
from flnd.ui.log import ui
from flnkit import browser as _browser
from flnkit import clock
from flnkit import net as _net


async def met(barrier):
    """Wait for the barrier's other parties; one that waits 10 s breaks it for them all."""
    try:
        await asyncio.wait_for(barrier.wait(), 10)
    except TimeoutError:
        await barrier.abort()
        raise


class GroupsTest(EngineCase):
    def test_an_http_plugin_lists_4_targets_at_once_and_a_browser_plugin_one(self):
        running, peaks = {"aaa": 0, "bbb": 0}, {"aaa": 0, "bbb": 0}
        # The first four of the HTTP plugin's six wait for each other: they list together.
        together = asyncio.Barrier(4)

        async def files(ctx, context):
            # The listing's first step, after its route: a target lists from its route on.
            code = ctx.target.plugin
            running[code] += 1
            peaks[code] = max(peaks[code], running[code])
            try:
                if code == "aaa" and ctx.target.value in "1234":
                    await met(together)
                for _ in range(20):
                    await asyncio.sleep(0)
            finally:
                running[code] -= 1
            return []

        http = make_plugin("aaa", files=files)
        browser = make_plugin("bbb", files=files, browser=True)
        self.run_engine(http, browser, targets=[*(f"aaa:{n}" for n in range(1, 7)),
                                                "bbb:1", "bbb:2", "bbb:3"])
        self.assertEqual(peaks, {"aaa": 4, "bbb": 1})

    def test_targets_of_other_plugins_never_wait_behind_a_browser_plugins_place(self):
        # The browser plugin's second target waits for its one place; the HTTP plugin's
        # target, started after it, lists at once, while the first still lists.
        started, seen = asyncio.Event(), []

        async def slow(ctx, context):
            if ctx.target.value == "1":
                seen.append(await asyncio.wait_for(started.wait(), 5))
            return []

        browser = make_plugin("bbb", files=slow, browser=True)
        self.run_engine(browser, make_plugin("aaa", lambda ctx: started.set()),
                        targets=["bbb:1", "bbb:2", "aaa:1"])
        self.assertEqual(seen, [True])

    def test_the_run_lists_its_active_targets_at_once_whatever_its_cores(self):
        entered, running, peak = [], [0], [0]
        # `ACTIVE` listings wait for each other.
        together = asyncio.Barrier(jobs.ACTIVE)

        async def files(ctx, context):
            entered.append(ctx.target)
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            try:
                if len(entered) <= jobs.ACTIVE:
                    await met(together)
                for _ in range(20):
                    await asyncio.sleep(0)
            finally:
                running[0] -= 1
            return []

        sources = [make_plugin(code, files=files) for code in ("paa", "pbb", "pcc", "pdd",
                                                               "pee", "pff")]
        self.enterContext(mock.patch.object(jobs.os, "cpu_count", return_value=2))
        self.run_engine(*sources, targets=[f"{source.code}:{n}" for source in sources
                                           for n in range(4)])
        self.assertEqual((len(entered), peak[0]), (24, jobs.ACTIVE))

    def test_two_targets_of_one_file_save_it_once_and_a_target_named_twice_runs_once(self):
        listed = []

        def files(ctx, context):
            listed.append(ctx.target.value)
            return [File(f"{CDN}/a.jpg", name="a.jpg")]

        source = make_plugin("aaa", files=files)
        report = self.report(source, targets=["aaa:x", "aaa:y", "aaa:x"])
        self.assertEqual(([str(one.target) for one in report.targets], report.files),
                         (["aaa:x", "aaa:y"], 1))
        # One key: the second target finds it queued by the first, as it would find it saved.
        self.assertEqual(len([r for r in self.requests if r.url.path == "/a.jpg"]), 1)
        self.assertEqual(sorted(listed), ["x", "y"])
        self.assertEqual(list(fakes.history(self.home)), ["aaa:a.jpg"])

    def test_targets_with_their_own_options_run_sequentially_and_targets_db_opens_once(self):
        seen, opened = [], []
        real_download, real_open = job.download, _store.open

        async def download(run, target, row, options, **listed):
            seen.append((target.value, options.again))
            return await real_download(run, target, row, options, **listed)

        def opening(home, **how):
            # The save's commit connection (`loop=True`) is its own.
            if not how.get("loop"):
                opened.append(home)
            return real_open(home, **how)

        sources = {"aaa": make_plugin("aaa"), "bbb": make_plugin("bbb")}
        engine = Engine(home=self.home, sources=sources)
        chosen = [(Target("aaa:a", kind="user"), Options(again=True)),
                  (Target("aaa:b", kind="user"), Options()), (Target("bbb:c", kind="user"),
                                                              Options())]
        stop = _run.Stop()
        with mock.patch.object(job, "download", download), mock.patch.object(_store, "open",
                                                                             opening):
            reports = fakes.run_targets(engine, chosen, stop).targets
        self.assertEqual(sorted(seen), [("a", True), ("b", False), ("c", False)])
        self.assertEqual(opened, [self.home])
        self.assertEqual([report.target for report in reports], [target for target, _ in chosen])

    def test_rows_end_at_ok_or_error_and_the_run_goes_on(self):
        def run(ctx):
            if ctx.target.value == "bad":
                raise Blocked("blocked")

        def files(ctx, context):
            return [File(f"{CDN}/{ctx.target.value}.jpg", name="a.jpg")]

        total = self.run_engine(make_plugin("aaa", run, files=files),
                                targets=["aaa:bad", "aaa:good"])
        self.assertEqual(total, 1)
        self.assertEqual(self.failures(), ["aaa:bad: blocked"])
        self.assertIn(("aaa:good", "posts"), self.listed())


    def test_a_group_whose_connect_fails_ends_its_rows_failed(self):
        # Fix 5: before it, only the error line was written and log.group ended the rows at ok.
        def connect(self, ctx):
            raise LoginError("tlg: the saved login no longer works")

        report = self.report(make_plugin("aaa", connect=connect), make_plugin("bbb"),
                             targets=["aaa:a", "aaa:b", "bbb:c"])
        self.assertEqual(self.failures(), ["aaa:a: tlg: the saved login no longer works",
                                           "aaa:b: tlg: the saved login no longer works"])
        self.assertEqual(self.listed(), {("bbb:c", "posts")})
        self.assertEqual([str(target) for target in report.failed], ["aaa:a", "aaa:b"])
        self.assertEqual(str(report.targets[0].error), "tlg: the saved login no longer works")

    def test_a_group_whose_logged_in_raises_ends_its_rows_failed(self):
        def check(self, ctx):
            raise RuntimeError("can't read the login")

        report = self.report(make_plugin("aaa", login=own_login(check=check)), targets=["aaa:a"])
        self.assertFalse(any(line.startswith("✓ aaa:a") for line in self.lines()), self.lines())
        self.assertEqual([str(target) for target in report.failed], ["aaa:a"])

    def test_failed_lists_only_the_targets_that_failed(self):
        def run(ctx):
            if ctx.target.value == "bad":
                raise Blocked("blocked")

        source = make_plugin("aaa", run)
        self.assertEqual(self.report(source, targets=["aaa:good"]).failed, ())
        report = self.report(source, targets=["aaa:bad", "aaa:good"])
        # Each report is its own run's: nothing piles up across runs, as `Engine.failed` once did.
        self.assertEqual([str(target) for target in report.failed], ["aaa:bad"])
        self.assertEqual(str(report.targets[0].error), "blocked")
        self.assertFalse(report.ok)


class LoginStateTest(EngineCase):
    def test_not_logged_in_is_a_hint_and_public_access_goes_on(self):
        ran = []
        self.run_engine(make_plugin("aaa", ran.append, login=login_cookies("sid")),
                        targets=["aaa:a"])
        self.assertEqual(len(ran), 1)
        self.assertIn("! aaa: not logged in, using public access; run flnd auth login aaa",
                      self.lines())

    def test_logged_in_has_no_hint_and_cookies_reach_the_client(self):
        self.log_in("aaa", sid="1", other="2")
        seen = []

        async def route(ctx):
            me = await ctx.fetch("https://api.aaa.example/me")
            seen.append((dict(ctx.cookies), me.json))

        source = make_plugin("aaa", route, login=login_cookies("sid"))
        self.routes["https://api.aaa.example/me"] = _net.reply(200, json={"me": 1})
        self.run_engine(source, targets=["aaa:a"])
        self.assertEqual(seen, [({"sid": "1", "other": "2"}, {"me": 1})])
        self.assertEqual(self.requests[0].headers["cookie"], "sid=1; other=2")
        self.assertFalse(any("not logged in" in line for line in self.lines()))

    def test_the_cookies_the_site_rotated_are_saved_in_the_logins_file_after_the_group(self):
        self.log_in("aaa", sid="1", ct0="old")
        source = make_plugin("aaa", lambda ctx: ctx.fetch("https://api.aaa.example/me"),
                             login=login_cookies("sid"))
        self.routes["https://api.aaa.example/me"] = _net.reply(200, json={}, headers=[
            ("set-cookie", "ct0=new-ct0; Domain=.aaa.example; Path=/"),
            ("set-cookie", "_tracker=t1; Domain=.aaa.example; Path=/")])
        self.run_engine(source, targets=["aaa:a", "aaa:b"])
        saved = vaulted("aaa")[0]
        self.assertEqual((saved["ct0"], saved["sid"], "_tracker" in saved), ("new-ct0", "1", False))
        self.assertNotIn("new-ct0", "\n".join(self.lines()))

    def test_a_failed_group_still_saves_the_rotated_cookies(self):
        self.log_in("aaa", sid="1", ct0="old")

        async def route(ctx):
            await ctx.fetch("https://api.aaa.example/me")
            raise Blocked("blocked")

        self.routes["https://api.aaa.example/me"] = _net.reply(
            200, json={}, headers=[("set-cookie", "ct0=new-ct0; Domain=.aaa.example; Path=/")])
        self.run_engine(make_plugin("aaa", route, login=login_cookies("sid")), targets=["aaa:a"])
        self.assertEqual(vaulted("aaa")[0]["ct0"], "new-ct0")

    def test_requires_login_fails_the_targets(self):
        ran = []
        self.run_engine(make_plugin("aaa", ran.append, login=login_cookies("sid", required=True)),
                        targets=["aaa:a", "aaa:b"])
        self.assertEqual(ran, [])
        for name in ("aaa:a", "aaa:b"):
            # The message, and the hint: what to do, never a traceback.
            self.assertIn(f"{name}: not logged in", self.failures())
        self.assertEqual({event.error.hint for event in self.events if isinstance(event, Failed)},
                         {"run flnd auth login aaa"})

    def test_download_never_asks(self):
        # The `Ctx` a download gives refuses `ask`, so a plugin can't stop to ask a question.
        asking = []

        async def route(ctx):
            try:
                await ctx.ask("Code")
            except UsageError as error:
                asking.append(str(error))

        self.run_engine(make_plugin("aaa", route), targets=["aaa:a"])
        self.assertEqual(asking, ["aaa: nobody can answer 'Code' here"])


class HttpTest(EngineCase):
    def test_one_client_per_group_with_headers_user_agent_and_signing(self):
        def sign(self, request, cookies):
            return request.replace(headers={**(request.headers or {}), "x-signed": "yes"})

        def run(ctx):
            return ctx.fetch(Request("https://api.aaa.example/a", headers={"x-extra": "1"}))

        source = make_plugin("aaa", run, user_agent="Agent/1", sign=sign)
        self.routes["https://api.aaa.example/a"] = _net.reply(200, json={})
        with mock.patch.object(_net, "Session", wraps=_net.Session) as made:
            self.run_engine(source, targets=["aaa:a", "aaa:b"])
        made.assert_called_once()
        request = self.requests[0]
        self.assertEqual((request.headers["user-agent"], request.headers["x-signed"],
                          request.headers["x-extra"]), ("Agent/1", "yes", "1"))

    def test_fetch_errors_and_soft(self):
        results = []

        async def run(ctx):
            soft = await ctx.fetch(Request("https://api.aaa.example/gone", check=False))
            results.append(soft.status)
            for url in ("https://api.aaa.example/gone", "https://api.aaa.example/html"):
                try:
                    (await ctx.fetch(url)).json
                except SiteError as error:
                    results.append((error.status, str(error)[:40]))
                except Error as error:
                    results.append((None, str(error)[:40]))
            results.append((await ctx.fetch("https://api.aaa.example/html")).html.first("h1").text)

        self.routes["https://api.aaa.example/gone"] = _net.reply(404)
        self.routes["https://api.aaa.example/html"] = _net.reply(200, html="<h1>Hi</h1>")
        self.run_engine(make_plugin("aaa", run), targets=["aaa:a"])
        self.assertEqual(results, [404, (404, "GET https://api.aaa.example/gone: HTTP 4"),
                                   (200, "expected JSON from https://api.aaa.examp"), "Hi"])

    def test_a_rate_limit_never_stops_the_plugin(self):
        ran = []
        answers = [_net.reply(429)] * 2 + [_net.reply(200, json={"ok": 1})]

        async def run(ctx):
            ran.append((ctx.target.value, (await ctx.fetch("https://api.aaa.example/feed")).json))

        self.routes["https://api.aaa.example/feed"] = (
            lambda request: answers.pop(0) if len(answers) > 1 else answers[0])
        self.run_engine(make_plugin("aaa", run), targets=["aaa:a", "aaa:b", "aaa:c"])
        self.assertEqual(sorted(ran), [("a", {"ok": 1}), ("b", {"ok": 1}), ("c", {"ok": 1})])
        self.assertIn("rate limited; waiting", " ".join(self.lines()))
        self.assertIn(("aaa:c", "posts"), self.listed())

    def test_a_rate_limited_query_lets_other_targets_run_while_waiting(self):
        ran = []

        class Query(Plugin):
            code, name, domains, api, pace = "qqq", "Q", ("api.qqq.example",), 2, 0.0
            browser = Browser.NONE

            @route("qqq?{words}")
            async def search(self, ctx, words):
                await ctx.fetch("https://api.qqq.example/search")
                if False:
                    yield Target("qqq:never")

            @route("qqq:{user}")
            async def user(self, ctx, user):
                return None

            @user.category
            async def posts(self, ctx, profile):
                if False:
                    yield Post("p", ())

        started = threading.Event()

        async def other(ctx):
            ran.append(("other", clock.now()))
            started.set()

        self.routes["https://api.qqq.example/search"] = _net.reply(429)
        engine = Engine(home=self.home,
                        sources={"qqq": Query, "bbb": make_plugin("bbb", other)})
        real_pause = clock.pause

        async def yielding_pause(seconds, *, stop=None):
            # The fake clock passes a wait at once, while bbb's start takes real thread hops:
            # a wait gives bbb up to 2 s of real time to start before its fake time passes.
            for _ in range(200):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            return await real_pause(seconds, stop=stop)

        with mock.patch.object(clock, "pause", yielding_pause):
            report = engine.download(["qqq?cats", "bbb:example_user"], folder="{target}/{category}")
        self.assertFalse(report.ok)
        self.assertIsInstance(report.targets[0].error, Blocked)
        self.assertEqual(ran, [("other", 0.0)])

    def test_challenges_are_waited_out_by_rounds_of_the_plugin(self):
        self.run_engine(make_plugin("aaa", lambda ctx: ctx.hold("challenged")),
                        targets=["aaa:a", "aaa:b", "aaa:c", "aaa:d"])
        waits = [line for line in self.lines() if "challenged; waiting" in line]
        # The rounds are the plugin's, across its targets: 30, 120 and 480 s, then the
        # fourth in a row fails its target (`Blocked`).
        self.assertEqual([wait.rsplit(" ", 1)[1] for wait in waits], ["30.0s", "2m", "8m"])
        # Its targets list at once, so any one of them meets the fourth.
        self.assertEqual(len(self.listed()), 3)

    def test_ctrl_c_during_a_wait_stops_the_run(self):
        """The first Ctrl-C ends a rate-limit wait at once; the target ends as stopped and the
        command as Ctrl-C."""
        stop_ref: list[_run.Stop] = []
        real_stopping = _run.stopping

        @contextmanager
        def capture_stopping(state=None):
            with real_stopping(state) as stop:
                stop_ref.append(stop)
                yield stop

        def limited(request):
            stop_ref[0].soon.set()
            return _net.reply(429)

        async def run(ctx):
            return (await ctx.fetch("https://api.aaa.example/feed")).json

        self.routes["https://api.aaa.example/feed"] = limited
        engine = Engine(home=self.home, sources={"aaa": make_plugin("aaa", run)})
        with mock.patch("flnd.engine._engine.stopping",
                        capture_stopping), self.assertRaises(KeyboardInterrupt), \
                ui.scope(level="debug"):
            engine.download(["aaa:a|all"])
        self.assertTrue(any(line.endswith("aaa:a: stopped") for line in self.lines()), self.lines())


class ChannelTest(unittest.TestCase):
    """The run's event channel: producers on the loop wait for room; `END` comes last."""

    def events(self, count, *, then=None):
        async def work(emit, stop):
            for number in range(count):
                await emit(Skipped(Target("aaa:a"), "posts", str(number)))
            if then is not None:
                await then()

        return _run.Run(work, name="events")

    def test_every_event_reaches_the_reader_before_the_end(self):
        # E10: the events a full channel held back are read before the end, never lost behind it.
        count = _run.QUEUE + 100
        run = self.events(count)
        self.assertEqual([event.post for event in run], [str(number) for number in range(count)])
        self.assertEqual(run.report.skipped, count)

    def test_a_reader_that_doesnt_read_holds_at_most_a_channel_of_events(self):
        # E11: past `QUEUE` events, the producer waits on the loop instead of queueing more.
        run = self.events(4 * _run.QUEUE)
        reading = iter(run)
        next(reading)
        deadline = time.monotonic() + 10
        while not run._channel._waiting and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(run._channel._waiting, "the producer never waited for room")
        self.assertEqual(len(run._channel), _run.QUEUE)
        self.assertEqual(len(list(reading)), 4 * _run.QUEUE - 1)


class PacingTest(EngineCase):
    """A run read by a slow reader is paced by it."""

    def setUp(self):
        super().setUp()
        self.listed = []
        test = self

        class Many(Plugin):
            code, name, domains, api, folder, key = "mny", "Many", ("mny.example",), 2, "", "{post}"

            @route("mny:{user}", newest_first=False)
            async def user(self, ctx, user):
                return user

            @user.category(newest_first=False)
            async def posts(self, ctx, user):
                for number in range(20):
                    test.listed.append(number)
                    # Dated, so no post is held back for its day.
                    yield Post(str(number), [File(f"{CDN}/{number}.jpg", name=f"{number}.jpg")],
                               date=1767312000)

        self.Many = Many

    def run_of(self):
        async def work(emit, stop):
            engine = Engine(home=self.home, sources={"mny": self.Many}, emit=emit, stop=stop)
            await engine.run_download(["mny:example_user"])

        return _run.Run(work, name="paced")

    def until(self, condition):
        deadline = time.monotonic() + 10
        while not condition():
            self.assertLess(time.monotonic(), deadline, "timed out")
            time.sleep(0.01)

    def test_a_reader_that_stops_reading_pauses_the_listing(self):
        fakes.run_targets(Engine(home=self.home, sources={"mny": self.Many}),
                          [(Target("mny:example_user", kind="user"), Options())], _run.Stop())
        self.listed.clear()
        self.enterContext(mock.patch.object(_run, "QUEUE", 2))
        run = self.run_of()
        reading = iter(run)
        next(reading)
        self.until(lambda: run._channel._waiting)
        # Every post is saved, so each is one `Skipped`: the listing waits for the reader.
        self.assertLess(len(self.listed), 20)
        self.assertEqual(len(list(reading)), 19)
        self.assertEqual(len(self.listed), 20)


class CtrlCTest(unittest.TestCase):
    def test_the_first_ctrl_c_stops_gracefully_and_the_second_interrupts(self):
        """D8: the first only sets `soon`, so the running downloads finish; the second stops now."""
        before = signal.getsignal(signal.SIGINT)
        with _run.stopping() as stop:
            self.assertFalse(stop.soon.is_set() or stop.now.is_set())
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)
            self.assertTrue(stop.soon.is_set())
            self.assertFalse(stop.now.is_set())
            handler(signal.SIGINT, None)
            self.assertTrue(stop.now.is_set())
        self.assertIs(signal.getsignal(signal.SIGINT), before)

    def stuck(self, *signals, work=None):
        """How long `Run.wait` took, and what it raised, for a run whose one job waits on the
        loop for ever (as a browser feed's step does), `signals` sent to the process while the
        reader waits. An unfixed run is freed after 8 s, so the test fails, never hangs."""
        held, done = [], threading.Event()

        async def waits_for_ever(emit, stop):
            loop = asyncio.get_running_loop()
            held.append((loop, loop.create_future()))
            with jobs.Scheduler(stop=stop) as scheduler:
                await scheduler.on(jobs.Kind.CALL, _browser.run_on, loop, held[0][1])

        def send():
            for sent in signals:
                time.sleep(0.2)
                os.kill(os.getpid(), sent)
            if not done.wait(8) and held:
                loop, never = held[0]
                loop.call_soon_threadsafe(never.set_result, None)

        # SIGTERM's own action would end the test process.
        self.enterContext(_signal_kept(signal.SIGTERM))
        threading.Thread(target=send, daemon=True).start()
        began = time.monotonic()
        try:
            _run.Run(work or waits_for_ever, name="stuck").wait()
            raised = None
        except BaseException as error:  # noqa: BLE001 (returned for the test to check)
            raised = error
        done.set()
        return time.monotonic() - began, raised

    def test_the_second_ctrl_c_or_a_sigterm_ends_a_run_stuck_in_a_thread(self):
        from flnd.errors import Stopped

        for signals in ((signal.SIGINT, signal.SIGINT), (signal.SIGTERM,)):
            with self.subTest(signals=signals):
                took, raised = self.stuck(*signals)
                self.assertIsInstance(raised, Stopped)
                self.assertLess(took, 5)

    def test_a_run_that_ignores_its_cancel_quits_the_process(self):
        class Quit(BaseException):
            """What the patched `os._exit` raises, so the test sees the exit."""

        ended = threading.Event()
        self.addCleanup(ended.set)

        async def deaf(emit, stop):
            # Deaf to its cancel: it goes on until the test ends.
            while not ended.is_set():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(0.01)

        with mock.patch.object(_run.os, "_exit", side_effect=Quit) as exit_now, \
                mock.patch.object(_run, "CANCEL_AFTER", 0.2), \
                mock.patch.object(_run, "GRACE", 0.6):
            took, raised = self.stuck(signal.SIGINT, signal.SIGINT, work=deaf)
        self.assertIsInstance(raised, Quit)
        exit_now.assert_called_once_with(130)
        self.assertLess(took, 5)


@contextmanager
def _signal_kept(number):
    """A handler for `number` that does nothing, put back to the old one afterwards."""
    before = signal.signal(number, lambda *_: None)
    try:
        yield
    finally:
        signal.signal(number, before)


def history(home):
    return fakes.history(home)


class ClientTest(EngineCase):
    def test_connect_is_entered_once_per_group(self):
        events = []

        @asynccontextmanager
        async def connect(self, ctx):
            events.append("enter")
            yield
            events.append("exit")

        source = make_plugin("aaa", lambda ctx: events.append("listed"), connect=connect)
        self.run_engine(source, targets=["aaa:a", "aaa:b"])
        self.assertEqual(events, ["enter", "listed", "listed", "exit"])

    def test_an_error_of_connects_exit_is_reported(self):
        # E16: the targets were saved, but a client that didn't close is the run's failure.
        @asynccontextmanager
        async def connect(self, ctx):
            yield
            raise RuntimeError("the client didn't close")

        files = [File(f"{CDN}/a.jpg", name="a.jpg")]
        report = self.report(make_plugin("aaa", connect=connect, files=files), targets=["aaa:a"])
        self.assertTrue(report.ok)
        [failed] = [event for event in self.events if isinstance(event, Failed)]
        self.assertEqual((str(failed.target), str(failed.error)),
                         ("aaa", "unexpected RuntimeError: the client didn't close"))

    def test_connect_the_route_and_a_fetch_file_share_one_loop(self):
        loops = []

        @asynccontextmanager
        async def connect(self, ctx):
            loops.append(asyncio.get_running_loop())
            yield

        async def fetch(part):
            loops.append(asyncio.get_running_loop())
            await part.write(b"media")

        async def run(ctx):
            loops.append(asyncio.get_running_loop())
            await asyncio.sleep(0)
            return "telethon"

        source = make_plugin("aaa", run, connect=connect, files=lambda ctx, client: [
            File(fetch=fetch, name=f"{client}.bin")])
        self.assertEqual(self.run_engine(source, targets=["aaa:a"]), 1)
        self.assertEqual(len(set(map(id, loops))), 1)
        self.assertEqual(len(loops), 3)
        self.assertEqual((self.target_dir("aaa", "a") / "telethon.bin").read_bytes(), b"media")

    def test_a_run_saves_more_fetch_files_than_the_pool_queues(self):
        # Each fetch needs the run's loop, so a save made on that loop must never wait for the pool.
        @asynccontextmanager
        async def connect(self, ctx):
            yield

        async def fetch(part):
            await part.write(b"media")

        files = [File(fetch=fetch, name=f"{number}.bin") for number in range(20)]
        source = make_plugin("aaa", connect=connect, limits={"aaa.example": 1}, files=files)
        finished = []
        runner = threading.Thread(
            target=lambda: finished.append(self.run_engine(source, targets=["aaa:a"])),
            daemon=True,
        )
        runner.start()
        runner.join(timeout=10)
        self.assertEqual(finished, [20])


class OffTheLoopTest(EngineCase):
    """flnd.db's connection waits up to 30 s for another process's lock: on the loop, that
    would stall every transfer of the run. Only the save's commit connection, which waits for no
    lock (`loop=True`), runs there."""

    def spy(self):
        """Each call of `Store`, of a plugin's `State` and of a file's stat made on the loop."""
        on_loop = []

        def spied(name, real):
            def call(*args, **kwargs):
                try:
                    asyncio.get_running_loop()
                    if not (isinstance(args[0], _store.Store) and args[0]._loop):
                        on_loop.append(name)
                except RuntimeError:
                    pass
                return real(*args, **kwargs)
            return call

        for owner, names in [
                (_store.Store, [name for name in vars(_store.Store) if not name.startswith("_")
                                and callable(getattr(_store.Store, name))]),
                (_store.State, ["__getitem__", "__setitem__", "__delitem__", "__iter__",
                                "__len__"])]:
            for name in names:
                self.enterContext(mock.patch.object(owner, name,
                                                    spied(name, getattr(owner, name))))
        self.enterContext(mock.patch.object(job, "_on_disk", spied("stat", job._on_disk)))
        return on_loop

    def test_flnd_db_and_the_disk_are_never_read_or_written_on_the_loop(self):
        on_loop = self.spy()
        files = [File(f"{CDN}/a.jpg", name="a.jpg"), File(f"{CDN}/b.txt", name="b.txt")]
        engine = Engine(home=self.home, sources={"aaa": make_plugin("aaa", files=files)})
        engine.download(["aaa:example_user"], sync=True)
        engine.sync()
        self.assertEqual(on_loop, [])

    def test_a_plugins_state_is_read_once_and_written_back_off_the_loop(self):
        def route(ctx):
            ctx.state["seen"] = ctx.state.get("seen", 0) + 1
            ctx.state.pop("gone", None)

        with _store.open(self.home) as db:
            db.state("aaa")["seen"], db.state("aaa")["gone"] = 1, True
        on_loop = self.spy()
        engine = Engine(home=self.home, sources={"aaa": make_plugin("aaa", route)})
        engine.download(["aaa:a", "aaa:b"])
        self.assertEqual(on_loop, [])
        with _store.open(self.home) as db:
            self.assertEqual(dict(db.state("aaa")), {"seen": 3})


class ApiTest(EngineCase):
    """The engine API of redesign B1: what `src/__init__.py` exports, `Report`, and
    `download`'s options."""

    def engine(self, *sources):
        return Engine(home=self.home, sources={source.code: source for source in sources})

    def saved(self):
        return fakes.targets(self.home)

    def test_report_files_failed_and_ok(self):
        good, bad = Target("aaa:good", kind="user"), Target("aaa:bad", kind="user")
        report = Results((TargetReport(good, (Path("a.jpg"), Path("b.jpg")), 1),
                         TargetReport(bad, (Path("c.jpg"),), 0, error="blocked")))
        self.assertEqual((report.files, report.failed, report.ok), (3, (bad,), False))
        self.assertTrue(Results((TargetReport(good, (), 2),)).ok)
        self.assertTrue(Results(()).ok)
        with self.assertRaises(AttributeError):
            report.targets = ()  # type: ignore[misc]

    def test_download_reports_each_target_in_order(self):
        def files(ctx, context):
            value = ctx.target.value
            if value == "bad":
                raise Blocked("blocked")
            return [File(f"{CDN}/{value}/a.jpg", name="a.jpg"),
                    File(f"{CDN}/{value}/b.jpg", name="b.jpg")]

        engine = self.engine(make_plugin("aaa", files=files))
        report = engine.download(["aaa:one|all", "aaa:bad|all"])
        one, bad = report.targets
        self.assertEqual((one.target, bad.target), (Target("aaa:one", kind="user"),
                                                    Target("aaa:bad", kind="user")))
        self.assertEqual(sorted(path.name for path in one.written), ["a.jpg", "b.jpg"])
        self.assertTrue(all(path.is_file() for path in one.written))
        self.assertEqual((one.skipped, one.error, str(bad.error)), (0, None, "blocked"))
        self.assertEqual((report.files, report.failed, report.ok), (2, (Target("aaa:bad",
                                                                               kind="user"),),
                                                                    False))
        again = engine.download([Target("aaa:one", kind="user")])
        self.assertEqual((again.files, again.targets[0].skipped, again.ok), (0, 2, True))

    def test_a_category_that_lists_to_its_end_is_recorded_complete(self):
        def files(ctx, context):
            if ctx.target.value == "cut":
                raise RuntimeError("cut short")
            return []

        self.engine(make_plugin("aaa", files=files)).download(["aaa:whole|all", "aaa:cut|all"])
        rows = self.saved()
        self.assertEqual(rows["aaa:whole"].complete, {"posts": "2026-01-01"})
        self.assertEqual(rows["aaa:cut"].complete, {})
        # Re-running keeps the record.
        self.engine(make_plugin("aaa")).download(["aaa:whole|all"])
        self.assertIn("posts", self.saved()["aaa:whole"].complete)

    def test_bad_values_are_value_errors_before_anything_starts(self):
        engine = self.engine(make_plugin("aaa"))
        cases: tuple[tuple[dict[str, Any], str], ...] = (
            ({"targets": ["aaa:a|reels"]}, "aaa has no category reels"),
            ({"limit": 0}, "--limit must be at least 1"),
        )
        for options, message in cases:
            with self.subTest(options=options), self.assertRaises(UsageError) as caught:
                engine.download(**{"targets": ["aaa:a"], **options})
            self.assertTrue(str(caught.exception).startswith(message), caught.exception)
        with self.assertRaisesRegex(UsageError, "no plugin named zzz"):
            engine.download(["zzz:a"])
        self.assertFalse((self.home / _store.MEDIA).exists())
        self.assertFalse((self.home / _store.DB).exists())

    def test_categories_on_a_single_post_are_refused_before_any_request(self):
        from flnd.engine.plugins import PLUGINS

        message = "tik:post/7312345678901234567 is one post, with no categories"
        engine = Engine(home=self.home, sources=PLUGINS)
        for targets in (["tik:7312345678901234567|videos"],
                        ["ins:example_user", "tik:7312345678901234567|all"]):
            with self.subTest(targets=targets), self.assertRaisesRegex(UsageError, f"^{message}$"):
                engine.download(targets)
        self.assertEqual(self.requests, [])
        self.assertFalse((self.home / _store.MEDIA).exists())
        self.assertFalse((self.home / _store.DB).exists())
        # A link to a post downloads it whole.
        runs = []
        async def run(runner, chosen, *, depth=0):
            runs.extend(options for _, options in chosen)
            return [TargetReport(target, (), 0) for target, _ in chosen]

        with mock.patch.object(_Runner, "run", run):
            self.assertTrue(engine.download(["https://www.instagram.com/p/C1abcDEFghi/"]).ok)
        self.assertEqual(runs, [Options()])

    def test_sync_downloads_files_deleted_by_hand(self):
        listed = {"a.jpg", "b.jpg", "c.jpg"}

        def files(ctx, context):
            return [File(f"{CDN}/{name}", name=name) for name in sorted(listed)]

        engine = self.engine(make_plugin("aaa", files=files))
        engine.download(["aaa:a|all"], sync=True)
        folder = self.target_dir("aaa", "a")
        (folder / "a.jpg").unlink()
        (folder / "c.jpg").unlink()
        listed.discard("c.jpg")
        report = engine.sync()
        [target] = report.targets
        self.assertEqual(target.target, Target("aaa:a", kind="user"))
        self.assertEqual(report.ok, True)


class LoginStaysAtHomeTest(EngineCase):
    """twt (`flnd.plugins.twt`) through the engine: what its API and its media servers each
    receive, with a `ct0` that the site rotates."""

    def setUp(self):
        super().setUp()
        from flnd.plugins.twt import QUERIES

        def url(query):
            return f"https://x.com/i/api/graphql/{QUERIES[query]}/{query}"

        jar = [{"name": name, "value": value, "domain": ".x.com", "path": "/"}
               for name, value in {"auth_token": "secret", "ct0": "old"}.items()]
        sign_in("twt", jar)
        user = {"data": {"user": {"result": {"rest_id": "42"}}}}
        self.routes[url("UserByScreenName")] = _net.reply(
            200, json=user, headers={"set-cookie": "ct0=new; Domain=x.com; Path=/"})
        media = [{"type": "photo", "media_url_https": "https://pbs.twimg.com/media/p.jpg"},
                 {"type": "video", "video_info": {"variants": [
                     {"content_type": "video/mp4", "bitrate": 9,
                      "url": "https://video.twimg.com/v/v.mp4"}]}}]
        legacy = {"id_str": "100", "user_id_str": "42", "extended_entities": {"media": media},
                  "created_at": "Mon Nov 09 10:00:00 +0000 2026"}
        self.routes[url("TweetResultByRestId")] = lambda request: _net.reply(200, json={
            "data": {"tweetResult": {"result": {"__typename": "Tweet", "rest_id": "100",
                                                "legacy": legacy}}}})

    def test_media_servers_never_see_the_login_and_the_api_sees_the_refreshed_token(self):
        from flnd.plugins.twt import TWITTER_BEARER, Twitter

        self.run_engine(Twitter, targets=["twt:example_handle",
                                          "https://x.com/example_handle/status/100"],
                        options=Options(categories=("profile",)))
        api = [request for request in self.requests if request.url.host == "x.com"]
        media = [request for request in self.requests if request.url.host.endswith("twimg.com")]
        self.assertEqual(sorted(request.url.host for request in media), ["pbs.twimg.com",
                                                                         "video.twimg.com"])
        for request in media:
            with self.subTest(host=request.url.host):
                self.assertNotIn("authorization", request.headers)
                self.assertNotIn("x-csrf-token", request.headers)
                self.assertNotIn("cookie", request.headers)
        self.assertEqual({request.headers["authorization"] for request in api}, {TWITTER_BEARER})
        # The first request carries the token it was saved with; x.com rotated it, and the rest
        # carry the new one.
        tokens = [request.headers["x-csrf-token"] for request in api]
        self.assertEqual((tokens[0], set(tokens[1:]), len(tokens) >= 2), ("old", {"new"}, True))
        self.assertIn("ct0=new", api[1].headers["cookie"])
        self.assertNotIn("ct0=old", api[1].headers["cookie"])
        folder = self.target_dir("twt", "100")
        self.assertTrue(any(path.name.endswith("_100_0.jpg") for path in folder.rglob("*")))


class GoneTest(EngineCase):
    def setUp(self):
        super().setUp()
        self.routes[f"{CDN}/a.jpg"] = _net.reply(200, content=noise("JPEG"))
        self.routes[f"{CDN}/b.jpg"] = _net.reply(404)
        self.source = make_plugin("aaa", files=[File(f"{CDN}/a.jpg", name="a.jpg"),
                                                File(f"{CDN}/b.jpg", name="b.jpg")])

    def asked(self):
        return sum(str(request.url) == f"{CDN}/b.jpg" for request in self.requests)

    def test_a_404_is_gone_and_asked_again_only_when_told(self):
        engine = Engine(home=self.home, sources={"aaa": self.source})
        engine.download(["aaa:example_user|all"])
        self.assertEqual(self.asked(), 1)
        with _store.open(self.home) as db:
            self.assertEqual(db.known("aaa:b.jpg")[1], "gone")
            self.assertEqual(db._db.all("SELECT key, class FROM failures", ()),
                             [("aaa:b.jpg", "NotFound")])
        engine.download(["aaa:example_user|all"])
        self.assertEqual(self.asked(), 1)
        engine.download(["aaa:example_user|all"], again=True)
        self.assertEqual(self.asked(), 2)

    def test_ls_sizes_come_from_the_database_never_a_walk(self):
        engine = Engine(home=self.home, sources={"aaa": self.source})
        engine.download(["aaa:example_user|all"], sync=True)
        with mock.patch.object(Path, "rglob", side_effect=AssertionError("a walk")), \
                mock.patch.object(os, "walk", side_effect=AssertionError("a walk")):
            [info] = engine.target_ls()
        self.assertEqual((info.files, info.size), (1, len(noise("JPEG"))))


if __name__ == "__main__":
    unittest.main()
