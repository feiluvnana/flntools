"""The run (src/engine/run.py) and the engine on it, on `flnkit.net.fake`, with test sources.

The event channel and `END`, then the engine's runs, plugin instances, HTTP, clients and logins
through its run (what `download` and `sync` await), then its commands: `download`, `sync`,
`login`, `logout` and `Report`. No network, no browser; the screen is a fake one.
"""

import asyncio
import signal
import sys
import threading
import time
import unittest
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

import fakes
from fakes import CDN, EngineCase, make_source, own_login

from flnd import Blocked, LoginError, Target, UsageError
from flnd.engine import crawl as job
from flnd.engine import home as _home
from flnd.engine import run as _run
from flnd.engine.core import Engine, Report, TargetReport, _Runner
from flnd.engine.events import Failed, Skipped
from flnd.engine.options import Options
from flnd.engine.site import LoginSite
from flnd.errors import Error
from flnd.source import File, Post, SiteError, Source, target
from flnd.source import cookies as login_cookies
from flnd.ui.log import ui
from flnkit import cookies as _cookies
from flnkit import net as _net


class GroupsTest(EngineCase):
    def test_same_source_targets_never_overlap_and_sources_run_together(self):
        lock = threading.Lock()
        running, peaks = {"aaa": 0, "bbb": 0}, {"aaa": 0, "bbb": 0, "all": 0}

        def run(site):
            code = site.target.source
            with lock:
                running[code] += 1
                peaks[code] = max(peaks[code], running[code])
                peaks["all"] = max(peaks["all"], sum(running.values()))
            time.sleep(0.05)
            with lock:
                running[code] -= 1

        first, second = make_source("aaa", run), make_source("bbb", run)
        self.run_engine(first, second, targets=["aaa:1", "aaa:2", "bbb:1", "aaa:3", "bbb:2"])
        self.assertEqual((peaks["aaa"], peaks["bbb"], peaks["all"]), (1, 1, 1))

    def test_targets_with_their_own_options_run_sequentially_and_targets_db_opens_once(self):
        seen, opened = [], []
        real_download, real_open = job.download, _home.open

        async def download(run, target, row, options):
            seen.append((target.value, options.again))
            return await real_download(run, target, row, options)

        def opening(home):
            opened.append(home)
            return real_open(home)

        sources = {"aaa": make_source("aaa"), "bbb": make_source("bbb")}
        engine = Engine(home=self.home, sources=sources)
        chosen = [(Target("aaa:a", kind="user"), Options(again=True)),
                  (Target("aaa:b", kind="user"), Options()), (Target("bbb:c", kind="user"), Options())]
        stop = _run.Stop()
        with mock.patch.object(job, "download", download), mock.patch.object(_home, "open", opening):
            reports = fakes.run_targets(engine, chosen, stop).targets
        self.assertEqual(seen, [("a", True), ("b", False), ("c", False)])
        self.assertEqual(opened, [self.home])
        self.assertEqual([report.target for report in reports], [target for target, _ in chosen])

    def test_rows_end_at_ok_or_error_and_the_run_goes_on(self):
        def run(site):
            if site.target.value == "bad":
                raise Blocked("blocked")

        def files(site, context):
            return [File(f"{CDN}/{site.target.value}.jpg", name="a.jpg")]

        total = self.run_engine(make_source("aaa", run, files=files), targets=["aaa:bad", "aaa:good"])
        self.assertEqual(total, 1)
        self.assertEqual(self.failures(), ["aaa:bad: blocked"])
        self.assertIn(("aaa:good", "posts"), self.listed())


    def test_a_group_whose_connect_fails_ends_its_rows_failed(self):
        # Fix 5: before it, only the error line was written and log.group ended the rows at ok.
        def connect(self, site):
            raise LoginError("tlg: the saved login no longer works")

        report = self.report(make_source("aaa", connect=connect), make_source("bbb"), targets=["aaa:a", "aaa:b", "bbb:c"])
        self.assertEqual(self.failures(), ["aaa:a: tlg: the saved login no longer works",
                                           "aaa:b: tlg: the saved login no longer works"])
        self.assertEqual(self.listed(), {("bbb:c", "posts")})
        self.assertEqual([str(target) for target in report.failed], ["aaa:a", "aaa:b"])
        self.assertEqual(str(report.targets[0].error), "tlg: the saved login no longer works")

    def test_a_group_whose_logged_in_raises_ends_its_rows_failed(self):
        def check(self, site):
            raise RuntimeError("can't read the login")

        report = self.report(make_source("aaa", login=own_login(check=check)), targets=["aaa:a"])
        self.assertFalse(any(line.startswith("✓ aaa:a") for line in self.lines()), self.lines())
        self.assertEqual([str(target) for target in report.failed], ["aaa:a"])

    def test_failed_lists_only_the_targets_that_failed(self):
        def run(site):
            if site.target.value == "bad":
                raise Blocked("blocked")

        source = make_source("aaa", run)
        self.assertEqual(self.report(source, targets=["aaa:good"]).failed, ())
        report = self.report(source, targets=["aaa:bad", "aaa:good"])
        # Each report is its own run's: nothing piles up across runs, as `Engine.failed` once did.
        self.assertEqual([str(target) for target in report.failed], ["aaa:bad"])
        self.assertEqual(str(report.targets[0].error), "blocked")
        self.assertFalse(report.ok)


class LoginStateTest(EngineCase):
    def test_not_logged_in_is_a_hint_and_public_access_goes_on(self):
        ran = []
        self.run_engine(make_source("aaa", ran.append, login=login_cookies("sid")), targets=["aaa:a"])
        self.assertEqual(len(ran), 1)
        self.assertIn("! aaa: not logged in, using public access; run flnd login aaa", self.lines())

    def test_logged_in_has_no_hint_and_cookies_reach_the_client(self):
        self.log_in("aaa", sid="1", other="2")
        seen = []
        source = make_source("aaa", lambda site: seen.append((dict(site.cookies),
                                                              site.fetch("https://api.aaa.example/me").json)),
                             login=login_cookies("sid"))
        self.routes["https://api.aaa.example/me"] = _net.reply(200, json={"me": 1})
        self.run_engine(source, targets=["aaa:a"])
        self.assertEqual(seen, [({"sid": "1", "other": "2"}, {"me": 1})])
        self.assertEqual(self.requests[0].headers["cookie"], "sid=1; other=2")
        self.assertFalse(any("not logged in" in line for line in self.lines()))

    def test_the_cookies_the_site_rotated_are_saved_in_the_vault_after_the_group(self):
        self.log_in("aaa", sid="1", ct0="old")
        source = make_source("aaa", lambda site: site.fetch("https://api.aaa.example/me"), login=login_cookies("sid"))
        self.routes["https://api.aaa.example/me"] = _net.reply(200, json={}, headers=[
            ("set-cookie", "ct0=new-ct0; Domain=.aaa.example; Path=/"),
            ("set-cookie", "_tracker=t1; Domain=.aaa.example; Path=/")])
        self.run_engine(source, targets=["aaa:a", "aaa:b"])
        saved = _home.load(self.home, "aaa")
        self.assertEqual((saved["ct0"], saved["sid"], "_tracker" in saved), ("new-ct0", "1", False))
        self.assertNotIn("new-ct0", "\n".join(self.lines()))

    def test_a_failed_group_still_saves_the_rotated_cookies(self):
        self.log_in("aaa", sid="1", ct0="old")

        def route(site):
            site.fetch("https://api.aaa.example/me")
            raise Blocked("blocked")

        self.routes["https://api.aaa.example/me"] = _net.reply(
            200, json={}, headers=[("set-cookie", "ct0=new-ct0; Domain=.aaa.example; Path=/")])
        self.run_engine(make_source("aaa", route, login=login_cookies("sid")), targets=["aaa:a"])
        self.assertEqual(_home.load(self.home, "aaa")["ct0"], "new-ct0")

    def test_requires_login_fails_the_targets(self):
        ran = []
        self.run_engine(make_source("aaa", ran.append, login=login_cookies("sid", required=True)), targets=["aaa:a", "aaa:b"])
        self.assertEqual(ran, [])
        for name in ("aaa:a", "aaa:b"):
            # The message, and the hint: what to do, never a traceback.
            self.assertIn(f"{name}: not logged in", self.failures())
        self.assertEqual({event.error.hint for event in self.events if isinstance(event, Failed)},
                         {"run flnd login aaa"})

    def test_download_never_asks(self):
        # The `Site` a download gives refuses `ask`, so a source can't stop to ask a question.
        asking = []

        def route(site):
            try:
                site.ask("Code")
            except UsageError as error:
                asking.append(str(error))

        self.run_engine(make_source("aaa", route), targets=["aaa:a"])
        self.assertEqual(asking, ["aaa: a run can't ask 'Code'"])


class HttpTest(EngineCase):
    def test_one_client_per_group_with_headers_user_agent_and_signing(self):
        def sign(self, request):
            request.headers["x-signed"] = "yes"

        def run(site):
            site.fetch("https://api.aaa.example/a", headers={"x-extra": "1"})

        source = make_source("aaa", run, user_agent="Agent/1", sign=sign)
        self.routes["https://api.aaa.example/a"] = _net.reply(200, json={})
        with mock.patch.object(_net, "Session", wraps=_net.Session) as made:
            self.run_engine(source, targets=["aaa:a", "aaa:b"])
        made.assert_called_once()
        request = self.requests[0]
        self.assertEqual((request.headers["user-agent"], request.headers["x-signed"], request.headers["x-extra"]),
                         ("Agent/1", "yes", "1"))

    def test_fetch_errors_and_soft(self):
        results = []

        def run(site):
            results.append(site.fetch("https://api.aaa.example/gone", check=False).status)
            for url in ("https://api.aaa.example/gone", "https://api.aaa.example/html"):
                try:
                    site.fetch(url).json
                except SiteError as error:
                    results.append((error.status, str(error)[:40]))
                except Error as error:
                    results.append((None, str(error)[:40]))
            results.append(site.fetch("https://api.aaa.example/html").html.first("h1").text)

        self.routes["https://api.aaa.example/gone"] = _net.reply(404)
        self.routes["https://api.aaa.example/html"] = _net.reply(200, html="<h1>Hi</h1>")
        self.run_engine(make_source("aaa", run), targets=["aaa:a"])
        self.assertEqual(results, [404, (404, "GET https://api.aaa.example/gone: HTTP 4"),
                                   (200, "expected JSON from https://api.aaa.examp"), "Hi"])

    def test_a_rate_limit_never_stops_the_source(self):
        ran = []
        answers = [_net.reply(429)] * 2 + [_net.reply(200, json={"ok": 1})]

        def run(site):
            ran.append((site.target.value, site.fetch("https://api.aaa.example/feed").json))

        self.routes["https://api.aaa.example/feed"] = lambda request: answers.pop(0) if len(answers) > 1 else answers[0]
        self.run_engine(make_source("aaa", run), targets=["aaa:a", "aaa:b", "aaa:c"])
        self.assertEqual(ran, [("a", {"ok": 1}), ("b", {"ok": 1}), ("c", {"ok": 1})])
        self.assertIn("rate limited; waiting", " ".join(self.lines()))
        self.assertIn(("aaa:c", "posts"), self.listed())

    def test_challenges_are_waited_out_by_rounds_of_the_account(self):
        self.run_engine(make_source("aaa", lambda site: site.back_off("challenged")),
                        targets=["aaa:a", "aaa:b", "aaa:c", "aaa:d"])
        waits = [line for line in self.lines() if "challenged; waiting" in line]
        # The rounds are the account's, across its targets: 30, 120 and 480 s, then the
        # fourth in a row fails its target (`Blocked`).
        self.assertEqual([wait.rsplit(" ", 1)[1] for wait in waits], ["30.0s", "2m", "8m"])
        self.assertEqual(self.listed(), {("aaa:a", "posts"), ("aaa:b", "posts"), ("aaa:c", "posts")})

    def test_ctrl_c_during_a_wait_stops_the_run(self):
        """The first Ctrl-C ends a rate-limit wait at once; the target ends as stopped and the command as Ctrl-C."""
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

        self.routes["https://api.aaa.example/feed"] = limited
        source = make_source("aaa", lambda site: site.fetch("https://api.aaa.example/feed").json)
        engine = Engine(home=self.home, sources={"aaa": source})
        with mock.patch("flnd.engine.core.stopping", capture_stopping), self.assertRaises(KeyboardInterrupt), \
                ui.scope(level="debug"):
            engine.download(["aaa:a"], categories="all")
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
    """A run read by a script: a slow reader paces it, and `suspend` holds every new job."""

    def setUp(self):
        super().setUp()
        self.listed = []
        test = self

        class Many(Source):
            code, name, domains, api, folder, key = "mny", "Many", ("mny.example",), 1, "", "{post}"

            @target("mny:{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
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

    def test_a_suspended_run_starts_no_job_until_it_resumes(self):
        run = self.run_of()
        run.suspend()
        got = []
        reader = threading.Thread(target=lambda: got.extend(run), daemon=True)
        reader.start()
        self.addCleanup(reader.join, 10)
        time.sleep(0.3)
        self.assertEqual((self.requests, self.listed), ([], []))
        run.resume()
        reader.join(10)
        self.assertEqual(run.report.saved, 20)


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


def history(home):
    return fakes.history(home)


class ClientTest(EngineCase):
    def test_sync_connect_is_entered_once_per_group(self):
        events = []

        @contextmanager
        def connect(self, site):
            events.append("enter")
            yield "client"
            events.append("exit")

        source = make_source("aaa", lambda site: events.append(site.client), connect=connect)
        self.run_engine(source, targets=["aaa:a", "aaa:b"])
        self.assertEqual(events, ["enter", "client", "client", "exit"])

    def test_an_error_of_connects_exit_is_reported(self):
        # E16: the targets were saved, but a client that didn't close is the run's failure.
        @contextmanager
        def connect(self, site):
            yield "client"
            raise RuntimeError("the client didn't close")

        files = [File(f"{CDN}/a.jpg", name="a.jpg")]
        report = self.report(make_source("aaa", connect=connect, files=files), targets=["aaa:a"])
        self.assertTrue(report.ok)
        [failed] = [event for event in self.events if isinstance(event, Failed)]
        self.assertEqual((str(failed.target), str(failed.error)),
                         ("aaa", "unexpected RuntimeError: the client didn't close"))

    def test_async_connect_run_and_fetch_share_one_loop(self):
        loops = []

        @asynccontextmanager
        async def connect(self, site):
            loops.append(asyncio.get_running_loop())
            yield "telethon"

        async def fetch(dest, progress):
            loops.append(asyncio.get_running_loop())
            dest.write_bytes(b"media")

        async def run(site):
            loops.append(asyncio.get_running_loop())
            await asyncio.sleep(0)
            return site.client

        source = make_source("aaa", run, connect=connect, files=lambda site, client: [File(fetch, name=f"{client}.bin")])
        self.assertEqual(self.run_engine(source, targets=["aaa:a"]), 1)
        self.assertEqual(len(set(map(id, loops))), 1)
        self.assertEqual(len(loops), 3)
        self.assertEqual((self.target_dir("aaa", "a") / "telethon.bin").read_bytes(), b"media")

    def test_an_async_run_saves_more_files_than_the_pool_queues(self):
        # Each fetch needs the source's loop, so a save made on that loop must never wait for the pool.
        @asynccontextmanager
        async def connect(self, site):
            yield "client"

        async def fetch(dest, progress):
            dest.write_bytes(b"media")

        async def run(site):
            pass

        source = make_source("aaa", run, connect=connect, limits={"aaa.example": 1},
                             files=[File(fetch, name=f"{number}.bin") for number in range(20)])
        finished = []
        runner = threading.Thread(target=lambda: finished.append(self.run_engine(source, targets=["aaa:a"])),
                                  daemon=True)
        runner.start()
        runner.join(timeout=10)
        self.assertEqual(finished, [20])


class EngineLoginTest(EngineCase):
    def test_a_source_with_its_own_login_gets_an_asking_session(self):
        seen = []

        def start(self, site, method):
            seen.append((method, type(site)))
            site.secrets["session"] = "s"

        source = make_source("aaa", login=own_login(start))
        self.assertEqual(Engine(home=self.home, sources={"aaa": source}).login("aaa", "qr"), "")
        self.assertEqual(seen, [("qr", LoginSite)])
        from flnd.engine import home as _home
        with _home.secrets(self.home, "aaa") as secrets:
            self.assertEqual(secrets["session"], "s")

    def test_interactive_only_during_login_in_a_terminal(self):
        seen = []

        def start(self, site, method):
            seen.append(site.interactive)
            site.secrets["session"] = "s"

        source = make_source("aaa", lambda site: seen.append(site.interactive), login=own_login(start))
        engine = Engine(home=self.home, sources={"aaa": source})
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            engine.login("aaa", "qr")
        with mock.patch.object(sys.stdin, "isatty", return_value=True):
            engine.login("aaa", "qr")
        self.run_engine(source, targets=["aaa:a"])
        self.assertEqual(seen, [False, True, False])

    def test_ask_shows_its_description_in_the_question(self):
        answers = []

        def start(self, site, method):
            answers.append(site.ask("Phone number", description="With the country code, such as +44"))

        source = make_source("aaa", login=own_login(start))
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch("flnd.ui.forms.ask", return_value="1") as asked:
            Engine(home=self.home, sources={"aaa": source}).login("aaa", "qr")
        self.assertEqual(answers, ["1"])
        asked.assert_called_once_with("Phone number", secret=False)
        self.assertIn("With the country code, such as +44", "\n".join(self.lines()))


class OffTheLoopTest(EngineCase):
    def test_flnd_db_and_the_disk_are_never_read_or_written_on_the_loop(self):
        # flnd.db's connection waits up to 30 s for another process's lock: on the loop, that
        # would stall every transfer of the run.
        on_loop = []

        def spied(name, real):
            def call(*args, **kwargs):
                try:
                    asyncio.get_running_loop()
                    on_loop.append(name)
                except RuntimeError:
                    pass
                return real(*args, **kwargs)
            return call

        names = [name for name in vars(_home.Home) if not name.startswith("_")
                 and callable(getattr(_home.Home, name))]
        for name in names:
            self.enterContext(mock.patch.object(_home.Home, name,
                                                spied(name, getattr(_home.Home, name))))
        self.enterContext(mock.patch.object(job, "_on_disk", spied("stat", job._on_disk)))
        files = [File(f"{CDN}/a.jpg", name="a.jpg"), File(f"{CDN}/b.txt", name="b.txt")]
        engine = Engine(home=self.home, sources={"aaa": make_source("aaa", files=files)})
        engine.download(["aaa:example_user"], sync=True)
        engine.sync()
        engine.retry()
        self.assertEqual(on_loop, [])


class ApiTest(EngineCase):
    """The engine API of redesign B1: what `src/__init__.py` exports, `Report`, and `download`'s options."""

    def engine(self, *sources):
        return Engine(home=self.home, sources={source.code: source for source in sources})

    def saved(self):
        return fakes.targets(self.home)

    def test_report_files_failed_and_ok(self):
        good, bad = Target("aaa:good", kind="user"), Target("aaa:bad", kind="user")
        report = Report((TargetReport(good, (Path("a.jpg"), Path("b.jpg")), 1),
                         TargetReport(bad, (Path("c.jpg"),), 0, error="blocked")))
        self.assertEqual((report.files, report.failed, report.ok), (3, (bad,), False))
        self.assertTrue(Report((TargetReport(good, (), 2),)).ok)
        self.assertTrue(Report(()).ok)
        with self.assertRaises(AttributeError):
            report.targets = ()  # type: ignore[misc]

    def test_download_reports_each_target_in_order(self):
        def files(site, context):
            if site.target.value == "bad":
                raise Blocked("blocked")
            return [File(f"{CDN}/{site.target.value}/a.jpg", name="a.jpg"), File(f"{CDN}/{site.target.value}/b.jpg",
                                                                                  name="b.jpg")]

        engine = self.engine(make_source("aaa", files=files))
        report = engine.download(["aaa:one", "aaa:bad"], categories="all")
        one, bad = report.targets
        self.assertEqual((one.target, bad.target), (Target("aaa:one", kind="user"), Target("aaa:bad", kind="user")))
        self.assertEqual(sorted(path.name for path in one.written), ["a.jpg", "b.jpg"])
        self.assertTrue(all(path.is_file() for path in one.written))
        self.assertEqual((one.skipped, one.error, str(bad.error)), (0, None, "blocked"))
        self.assertEqual((report.files, report.failed, report.ok), (2, (Target("aaa:bad", kind="user"),), False))
        again = engine.download([Target("aaa:one", kind="user")], categories="all")
        self.assertEqual((again.files, again.targets[0].skipped, again.ok), (0, 2, True))

    def test_a_category_that_lists_to_its_end_is_recorded_complete(self):
        def files(site, context):
            if site.target.value == "cut":
                raise RuntimeError("cut short")
            return []

        self.engine(make_source("aaa", files=files)).download(["aaa:whole", "aaa:cut"], categories="all")
        rows = self.saved()
        self.assertEqual(rows["aaa:whole"].complete, {"posts": "2026-01-01"})
        self.assertEqual(rows["aaa:cut"].complete, {})
        # Re-running keeps the record.
        self.engine(make_source("aaa")).download(["aaa:whole"], categories="all")
        self.assertIn("posts", self.saved()["aaa:whole"].complete)

    def test_bad_values_are_value_errors_before_anything_starts(self):
        engine = self.engine(make_source("aaa"))
        cases: tuple[tuple[dict[str, Any], str], ...] = (
            ({"categories": "reels"}, "no source here has 'reels'"),
            ({"limit": 0}, "--limit must be at least 1"),
        )
        for options, message in cases:
            with self.subTest(options=options), self.assertRaises(UsageError) as caught:
                engine.download(["aaa:a"], **options)
            self.assertTrue(str(caught.exception).startswith(message), caught.exception)
        with self.assertRaisesRegex(UsageError, "unknown source 'zzz'"):
            engine.download(["zzz:a"])
        self.assertFalse((self.home / _home.MEDIA).exists())
        self.assertFalse((self.home / _home.DB).exists())

    def test_from_with_a_post_link_is_refused_before_any_request(self):
        from flnd.sources import SOURCES

        message = ("--only applies to profiles and chats; ins:C1abcDEFghi is one post, downloaded whole: "
                   "give it without --only")
        engine = Engine(home=self.home, sources=SOURCES)
        for targets in (["https://www.instagram.com/p/C1abcDEFghi/"],
                        ["ins:example_user", "https://www.instagram.com/p/C1abcDEFghi/"],
                        ["tik:example_creator", "https://www.instagram.com/p/C1abcDEFghi/"]):
            with self.subTest(targets=targets), self.assertRaisesRegex(UsageError, f"^{message}$"):
                engine.download(targets, categories="all")
        self.assertEqual(self.requests, [])
        self.assertFalse((self.home / _home.MEDIA).exists())
        self.assertFalse((self.home / _home.DB).exists())
        # Without --from the same link downloads whole.
        runs = []
        async def run(runner, chosen, *, depth=0):
            runs.extend(options for _, options in chosen)
            return [TargetReport(target, (), 0) for target, _ in chosen]

        with mock.patch.object(_Runner, "run", run):
            self.assertTrue(engine.download(["https://www.instagram.com/p/C1abcDEFghi/"]).ok)
        self.assertEqual(runs, [Options()])

    def test_sync_downloads_files_deleted_by_hand(self):
        listed = {"a.jpg", "b.jpg", "c.jpg"}

        def files(site, context):
            return [File(f"{CDN}/{name}", name=name) for name in sorted(listed)]

        engine = self.engine(make_source("aaa", files=files))
        engine.download(["aaa:a"], categories="all", sync=True)
        folder = self.target_dir("aaa", "a")
        (folder / "a.jpg").unlink()
        (folder / "c.jpg").unlink()
        listed.discard("c.jpg")
        report = engine.sync()
        [target] = report.targets
        self.assertEqual(target.target, Target("aaa:a", kind="user"))
        self.assertEqual(report.ok, True)


class ApiLoginTest(EngineCase):
    """`login` and `logout`: every credential in the vault, and nothing else written (redesign B7)."""

    def setUp(self):
        super().setUp()
        self.source = make_source("aaa", login=login_cookies("sid"))
        self.engine = Engine(home=self.home, sources={"aaa": self.source, "bbb": make_source("bbb")})
        jar = _cookies.Jar([{"name": "sid", "value": "s", "domain": ".aaa.example", "path": "/"},
                           {"name": "other", "value": "o", "domain": ".example.com", "path": "/"}])
        patcher = mock.patch("flnkit.cookies.load", lambda browser="chrome", *, profile=None: jar)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_browser_login_lives_only_in_the_vault(self):
        # A source without a login (bbb) has nothing to log in to.
        self.assertEqual(self.engine.cookie_logins, ("aaa",))
        self.assertEqual(self.engine.login("aaa", "browser", browser="chrome"), "aaa: logged in from Chrome (1 cookie)")
        self.assertEqual(sorted(path.name for path in self.home.rglob("*") if path.is_file()), ["flnd.db", "secret.db"])
        self.assertEqual(self.engine.logins, {"aaa": "logged in"})
        self.assertEqual({cookie["name"] for cookie in _home.load(self.home, "aaa")}, {"sid"})

    def test_a_file_login_reads_the_file_once_and_never_copies_it(self):
        path = self.home.parent / "cookies.txt"
        path.write_text("# Netscape HTTP Cookie File\n.aaa.example\tTRUE\t/\tTRUE\t1825861327\tsid\tv\n")
        self.assertEqual(self.engine.login("aaa", "file", path=path), "aaa: logged in from cookies.txt (1 cookie)")
        self.assertTrue(path.is_file())
        self.assertEqual(sorted(path.name for path in self.home.rglob("*") if path.is_file()), ["flnd.db", "secret.db"])

    def test_a_failed_login_and_bad_answers(self):
        with mock.patch("flnkit.cookies.load",
                        lambda browser="chrome", *, profile=None: _cookies.Jar()):
            with self.assertRaisesRegex(LoginError, "no sid for aaa.example in Chrome"):
                self.engine.login("aaa", "browser", browser="chrome")
        for args, options in ((("aaa", "qr"), {}), (("zzz", "browser"), {}), (("aaa", "browser"), {}),
                              (("aaa", "file"), {})):
            with self.subTest(args=args), self.assertRaises(UsageError):
                self.engine.login(*args, **options)
        self.assertEqual(self.engine.logins, {})

    def test_logout_clears_the_jar_and_returns_the_codes_that_had_one(self):
        self.engine.login("aaa", "browser", browser="chrome")
        self.assertEqual(self.engine.logout(["bbb", "aaa", "aaa"]), ["aaa"])
        self.assertEqual(self.engine.logins, {})
        self.assertEqual(_home.load(self.home, "aaa"), _cookies.Jar())
        self.engine.login("aaa", "browser", browser="chrome")
        self.assertEqual(self.engine.logout(["all"]), ["aaa"])
        self.assertEqual(self.engine.logout(["all"]), [])
        with self.assertRaisesRegex(UsageError, "unknown source 'zzz'"):
            self.engine.logout(["zzz"])

    def test_a_download_uses_the_vaulted_login(self):
        self.engine.login("aaa", "browser", browser="chrome")
        seen = []
        self.engine = Engine(home=self.home, sources={"aaa": make_source("aaa", lambda site: seen.append(dict(site.cookies)),
                                                                         login=login_cookies("sid"))})
        self.assertTrue(self.engine.download(["aaa:a"], categories="all").ok)
        self.assertEqual(seen, [{"sid": "s"}])
        self.assertFalse(any("not logged in" in line for line in self.lines()))


class LoginStaysAtHomeTest(EngineCase):
    """twt through the engine: what its API and its media servers each receive, with a `ct0`
    that the site rotates (moved from tests/test_twt.py, which plugin API 2 replaces)."""

    def setUp(self):
        super().setUp()
        from flnd.sources.twt import QUERIES

        def url(query):
            return f"https://x.com/i/api/graphql/{QUERIES[query]}/{query}"

        jar = [{"name": name, "value": value, "domain": ".x.com", "path": "/"}
               for name, value in {"auth_token": "secret", "ct0": "old"}.items()]
        _home.save(self.home, "twt", _cookies.Jar(jar))
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
        from flnd.sources.twt import TWITTER_BEARER, Twitter

        self.run_engine(Twitter, targets=["twt:example_handle", "twt:https://x.com/example_handle/status/100"],
                        options=Options(categories=("profile",)))
        api = [request for request in self.requests if request.url.host == "x.com"]
        media = [request for request in self.requests if request.url.host.endswith("twimg.com")]
        self.assertEqual(sorted(request.url.host for request in media), ["pbs.twimg.com", "video.twimg.com"])
        for request in media:
            with self.subTest(host=request.url.host):
                self.assertNotIn("authorization", request.headers)
                self.assertNotIn("x-csrf-token", request.headers)
                self.assertNotIn("cookie", request.headers)
        self.assertEqual({request.headers["authorization"] for request in api}, {TWITTER_BEARER})
        # The first request carries the token it was saved with; x.com rotated it, and the rest carry the new one.
        tokens = [request.headers["x-csrf-token"] for request in api]
        self.assertEqual((tokens[0], set(tokens[1:]), len(tokens) >= 2), ("old", {"new"}, True))
        self.assertIn("ct0=new", api[1].headers["cookie"])
        self.assertNotIn("ct0=old", api[1].headers["cookie"])
        self.assertTrue(any(path.name.endswith("_100_0.jpg") for path in self.target_dir("twt", "100").rglob("*")))


if __name__ == "__main__":
    unittest.main()
