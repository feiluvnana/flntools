"""The engine end to end on httpx.MockTransport, with test sources. No network, no browser.

Its runs, groups, HTTP, clients and logins through `Engine._run` (what `download` and `sync` call
once per run), then the API itself (redesign B1): `download`, `sync`, `login`, `logout` and
`Report`, the options read from text, and `--from` refused for a post. The screen is a fake one.
"""

import asyncio
import datetime
import inspect
import io
import signal
import sys
import threading
import time
import unittest
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import mock

import httpx

from flnd import job, targets as targets_mod
from flnd.common import auth, net, paths
from flnd import Engine, LoginError, Range, Report, Target, TargetReport
from flnd.options import Options
from flnd.site import LoginSite
from flnd.source import FetchError, File, Post, Source, target
from shared import cli, clock, cookies, http, store, ui

CDN = "https://cdn.example"


def noise(format, size=(64, 48)):
    if format.upper() == "PNG":
        return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(32)
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + bytes(32)


def make_source(code, route=None, *, files: Any = (), **declared):
    """A routed test source: `code:<name>` reads a user, `route(site)` is its route, and `files` are its `posts`.

        make_source("aaa", lambda site: seen.append(site.target.value), files=[File(url, name="a.jpg")])

    Args:
        code: the source's code; its domain is `<code>.example`.
        route: called with the target's `Site` when the target is read; what it returns is the
            context of the category. A coroutine function makes the route and the category `async def`.
        files: the files `posts` yields, each in a post of its own, kept as the site names them and
            filed in the target's own folder, keyed by their path (`aaa:a.jpg`); or a function of
            `(site, context)` giving them.
        **declared: the class's other fields and methods, such as `needs`, `connect` or `prepare`.
    """
    def listed(site, context):
        found: Any = files(site, context) if callable(files) else files
        return [Post(file.name or str(number), [file]) for number, file in enumerate(found)]

    if inspect.iscoroutinefunction(route):
        @target("{user}")
        async def user(self, site, user):
            return await route(site)

        @user.category
        async def posts(self, site, context):
            for post in listed(site, context):
                yield post
    else:
        @target("{user}")
        def user(self, site, user):
            return route(site) if route is not None else None

        @user.category
        def posts(self, site, context):
            yield from listed(site, context)

    attributes = {"code": code, "name": code.upper(), "domains": (f"{code}.example",), "folder": "", "key": "{path}",
                  "user": user, "posts": posts, **declared}
    return type(f"Source_{code}", (Source,), attributes)


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.dest = io.StringIO()
        scope = ui.scope(dest=self.dest, width=400, color=False)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / ".flnd"
        self.requests = []
        self.routes = {}
        patcher = mock.patch.object(httpx.Client, "_init_transport", lambda *a, **kw: httpx.MockTransport(self.serve))
        patcher.start()
        self.addCleanup(patcher.stop)
        # Every wait (pacing, back-offs, retries) passes at once on the fake clock.
        self.fake = self.enterContext(clock.use(clock.Fake()))

    @property
    def now(self):
        return self.fake.now

    def serve(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.routes.get(str(request.url).split("?")[0])
        if callable(answer):
            return answer(request)  # type: ignore[return-value]
        if answer is None:
            return httpx.Response(200, content=request.url.path.encode())
        return answer

    def run_engine(self, *sources, targets, options=Options(), **settings):
        """Run the targets, each with `options`, and return how many files were written."""
        return self.report(*sources, targets=targets, options=options, **settings).files

    def report(self, *sources, targets, options=Options(), stop=None, **settings):
        """Run the targets, each with `options`, and return the `Report`."""
        engine = Engine(home=self.home, sources={source.code: source for source in sources}, **settings)
        stop = stop or cli.Stopping(threading.Event(), threading.Event())
        return Report(tuple(engine._run([(target, options) for target in engine.resolve(targets)], stop=stop)))

    def log_in(self, code, **values):
        jar = [{"name": name, "value": value, "domain": f".{code}.example", "path": "/"} for name, value in values.items()]
        auth.save(self.home, code, cookies.Jar(jar))

    def lines(self):
        return self.dest.getvalue().splitlines()

    def target_dir(self, code, name):
        return self.home / paths.MEDIA / code / name


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
        real_download, real_open = job.download, store.open

        def download(run, target, row, options):
            seen.append((target.value, options.again))
            return real_download(run, target, row, options)

        def opening(path, **kwargs):
            opened.append(Path(path).name)
            return real_open(path, **kwargs)

        sources = {"aaa": make_source("aaa"), "bbb": make_source("bbb")}
        engine = Engine(home=self.home, sources=sources)
        chosen = [(Target("aaa", "user", "a"), Options(again=True)),
                  (Target("aaa", "user", "b"), Options()), (Target("bbb", "user", "c"), Options())]
        stop = cli.Stopping(threading.Event(), threading.Event())
        with mock.patch.object(job, "download", download), mock.patch.object(store, "open", opening):
            reports = engine._run(chosen, stop=stop)
        self.assertEqual(seen, [("a", True), ("b", False), ("c", False)])
        self.assertEqual(opened.count(paths.TARGETS.name), 1)
        self.assertEqual([report.target for report in reports], [target for target, _ in chosen])

    def test_rows_end_at_ok_or_error_and_the_run_goes_on(self):
        def run(site):
            if site.target.value == "bad":
                raise RuntimeError("blocked")

        def files(site, context):
            return [File(f"{CDN}/{site.target.value}.jpg", name="a.jpg")]

        total = self.run_engine(make_source("aaa", run, files=files), targets=["aaa:bad", "aaa:good"])
        self.assertEqual(total, 1)
        lines = self.lines()
        self.assertTrue(any(line.endswith("aaa:bad: blocked") for line in lines), lines)
        self.assertTrue(any("aaa:good posts " in line for line in lines), lines)


    def test_a_group_whose_connect_fails_ends_its_rows_failed(self):
        # Fix 5: before it, only the error line was written and log.group ended the rows at ok.
        def connect(self, site):
            raise RuntimeError("tlg: the saved login no longer works")

        report = self.report(make_source("aaa", connect=connect), make_source("bbb"), targets=["aaa:a", "aaa:b", "bbb:c"])
        lines = self.lines()
        self.assertTrue(any(line.endswith("aaa: tlg: the saved login no longer works") for line in lines), lines)
        self.assertFalse(any("aaa:" in line and "posts " in line for line in lines), lines)
        self.assertTrue(any("bbb:c posts " in line for line in lines), lines)
        self.assertEqual([str(target) for target in report.failed], ["aaa:a", "aaa:b"])
        self.assertEqual(report.targets[0].error, "tlg: the saved login no longer works")

    def test_a_group_whose_logged_in_raises_ends_its_rows_failed(self):
        def logged_in(self, site):
            raise RuntimeError("can't read the login")

        report = self.report(make_source("aaa", logged_in=logged_in), targets=["aaa:a"])
        self.assertFalse(any(line.startswith("✓ aaa:a") for line in self.lines()), self.lines())
        self.assertEqual([str(target) for target in report.failed], ["aaa:a"])

    def test_failed_lists_only_the_targets_that_failed(self):
        def run(site):
            if site.target.value == "bad":
                raise RuntimeError("blocked")

        source = make_source("aaa", run)
        self.assertEqual(self.report(source, targets=["aaa:good"]).failed, ())
        report = self.report(source, targets=["aaa:bad", "aaa:good"])
        # Each report is its own run's: nothing piles up across runs, as `Engine.failed` once did.
        self.assertEqual([str(target) for target in report.failed], ["aaa:bad"])
        self.assertEqual(report.targets[0].error, "blocked")
        self.assertFalse(report.ok)


class LoginStateTest(EngineCase):
    def test_not_logged_in_is_a_hint_and_public_access_goes_on(self):
        ran = []
        self.run_engine(make_source("aaa", ran.append, needs=("sid",)), targets=["aaa:a"])
        self.assertEqual(len(ran), 1)
        self.assertIn("! aaa: not logged in, using public access; run flnd auth login", self.lines())

    def test_logged_in_has_no_hint_and_cookies_reach_the_client(self):
        self.log_in("aaa", sid="1", other="2")
        seen = []
        source = make_source("aaa", lambda site: seen.append((dict(site.cookies),
                                                              site.fetch("https://api.aaa.example/me").json)),
                             needs=("sid",))
        self.routes["https://api.aaa.example/me"] = httpx.Response(200, json={"me": 1})
        self.run_engine(source, targets=["aaa:a"])
        self.assertEqual(seen, [({"sid": "1", "other": "2"}, {"me": 1})])
        self.assertEqual(self.requests[0].headers["cookie"], "sid=1; other=2")
        self.assertFalse(any("not logged in" in line for line in self.lines()))

    def test_the_cookies_the_site_rotated_are_saved_in_the_vault_after_the_group(self):
        self.log_in("aaa", sid="1", ct0="old")
        source = make_source("aaa", lambda site: site.fetch("https://api.aaa.example/me"), needs=("sid",))
        self.routes["https://api.aaa.example/me"] = httpx.Response(200, json={}, headers=[
            ("set-cookie", "ct0=new-ct0; Domain=.aaa.example; Path=/"),
            ("set-cookie", "_tracker=t1; Domain=.aaa.example; Path=/")])
        self.run_engine(source, targets=["aaa:a", "aaa:b"])
        saved = auth.load(self.home, "aaa")
        self.assertEqual((saved["ct0"], saved["sid"], "_tracker" in saved), ("new-ct0", "1", False))
        self.assertNotIn("new-ct0", "\n".join(self.lines()))

    def test_a_failed_group_still_saves_the_rotated_cookies(self):
        self.log_in("aaa", sid="1", ct0="old")

        def route(site):
            site.fetch("https://api.aaa.example/me")
            raise RuntimeError("blocked")

        self.routes["https://api.aaa.example/me"] = httpx.Response(
            200, json={}, headers=[("set-cookie", "ct0=new-ct0; Domain=.aaa.example; Path=/")])
        self.run_engine(make_source("aaa", route, needs=("sid",)), targets=["aaa:a"])
        self.assertEqual(auth.load(self.home, "aaa")["ct0"], "new-ct0")

    def test_requires_login_fails_the_targets(self):
        ran = []
        self.run_engine(make_source("aaa", ran.append, needs=("sid",), requires_login=True), targets=["aaa:a", "aaa:b"])
        self.assertEqual(ran, [])
        self.assertTrue(any(line.endswith("aaa:a: not logged in; run flnd auth login") for line in self.lines()),
                        self.lines())
        self.assertTrue(any(line.endswith("aaa:b: not logged in; run flnd auth login") for line in self.lines()),
                        self.lines())

    def test_download_never_asks(self):
        # The `Site` a download gives has no `ask`, so a source can't stop to ask a question.
        asking = []
        self.run_engine(make_source("aaa", lambda site: asking.append(hasattr(site, "ask"))), targets=["aaa:a"])
        self.assertEqual(asking, [False])


class HttpTest(EngineCase):
    def test_one_client_per_group_with_headers_user_agent_and_signing(self):
        def prepare(self, request, cookies):
            request.headers["x-signed"] = "yes"
            return request

        def run(site):
            site.fetch("https://api.aaa.example/a", headers={"x-extra": "1"})

        source = make_source("aaa", run, user_agent="Agent/1", prepare=prepare)
        self.routes["https://api.aaa.example/a"] = httpx.Response(200, json={})
        with mock.patch.object(net, "client", wraps=net.client) as made:
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
                except FetchError as error:
                    results.append((error.status, str(error)[:40]))
                except http.Error as error:
                    results.append((None, str(error)[:40]))
            results.append(site.fetch("https://api.aaa.example/html").html.css("h1").text)

        self.routes["https://api.aaa.example/gone"] = httpx.Response(404)
        self.routes["https://api.aaa.example/html"] = httpx.Response(200, html="<h1>Hi</h1>")
        self.run_engine(make_source("aaa", run), targets=["aaa:a"])
        self.assertEqual(results, [404, (404, "GET https://api.aaa.example/gone: HTTP 4"),
                                   (None, "expected JSON from https://api.aaa.examp"), "Hi"])

    def test_a_rate_limit_never_stops_the_source(self):
        ran = []
        answers = [httpx.Response(429)] * 2 + [httpx.Response(200, json={"ok": 1})]

        def run(site):
            ran.append((site.target.value, site.fetch("https://api.aaa.example/feed").json))

        self.routes["https://api.aaa.example/feed"] = lambda request: answers.pop(0) if len(answers) > 1 else answers[0]
        self.run_engine(make_source("aaa", run), targets=["aaa:a", "aaa:b", "aaa:c"])
        self.assertEqual(ran, [("a", {"ok": 1}), ("b", {"ok": 1}), ("c", {"ok": 1})])
        self.assertIn("rate limited; waiting", " ".join(self.lines()))
        self.assertTrue(any("aaa:c posts " in line for line in self.lines()), self.lines())

    def test_challenges_are_waited_out(self):
        self.run_engine(make_source("aaa", lambda site: site.back_off("challenged")),
                        targets=["aaa:a", "aaa:b", "aaa:c", "aaa:d"])
        waits = [line for line in self.lines() if "challenged; waiting" in line]
        self.assertEqual(len(waits), 4)
        self.assertTrue(any("aaa:d posts " in line for line in self.lines()), self.lines())

    def test_ctrl_c_during_a_wait_stops_the_run(self):
        """The first Ctrl-C ends a rate-limit wait at once; the target ends as stopped and the command as Ctrl-C."""
        stop_ref: list[cli.Stopping] = []
        real_stopping = cli.stopping

        @contextmanager
        def capture_stopping():
            with real_stopping() as stop:
                stop_ref.append(stop)
                yield stop

        def limited(request):
            stop_ref[0].soon.set()
            return httpx.Response(429)

        self.routes["https://api.aaa.example/feed"] = limited
        source = make_source("aaa", lambda site: site.fetch("https://api.aaa.example/feed").json)
        engine = Engine(home=self.home, sources={"aaa": source})
        with mock.patch.object(cli, "stopping", capture_stopping), self.assertRaises(KeyboardInterrupt):
            engine.download(["aaa:a"], categories="all")
        self.assertTrue(any(line.endswith("aaa:a: stopped") for line in self.lines()), self.lines())


class CtrlCTest(unittest.TestCase):
    def test_the_first_ctrl_c_stops_gracefully_and_the_second_interrupts(self):
        """D8: the first only sets `soon`, so the running downloads finish; the second stops now."""
        before = signal.getsignal(signal.SIGINT)
        with cli.stopping() as stop:
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
    with paths.table(home / paths.HISTORY, "downloads") as rows:
        return dict(rows.items())


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

        source = make_source("aaa", run, connect=connect, concurrency=1,
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

        def own_login(self, login, method):
            seen.append((method, type(login)))
            login.secrets["session"] = "s"

        source = make_source("aaa", login=own_login, login_methods=("qr",))
        self.assertEqual(Engine(home=self.home, sources={"aaa": source}).login("aaa", "qr"), "")
        self.assertEqual(seen, [("qr", LoginSite)])
        from flnd.common import auth
        with auth.secrets(self.home, "aaa") as secrets:
            self.assertEqual(secrets["session"], "s")

    def test_interactive_only_during_login_in_a_terminal(self):
        seen = []

        def own_login(self, login, method):
            seen.append(login.interactive)

        source = make_source("aaa", lambda site: seen.append(hasattr(site, "interactive")), login=own_login,
                             login_methods=("qr",))
        engine = Engine(home=self.home, sources={"aaa": source})
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            engine.login("aaa", "qr")
        with mock.patch.object(sys.stdin, "isatty", return_value=True):
            engine.login("aaa", "qr")
        self.run_engine(source, targets=["aaa:a"])
        self.assertEqual(seen, [False, True, False])

    def test_ask_shows_its_description_in_the_question(self):
        answers = []

        def own_login(self, login, method):
            answers.append(login.ask("Phone number", description="With the country code, such as +44"))

        source = make_source("aaa", login=own_login, login_methods=("qr",))
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch("shared.prompt.ask", return_value="1") as asked:
            Engine(home=self.home, sources={"aaa": source}).login("aaa", "qr")
        self.assertEqual(answers, ["1"])
        asked.assert_called_once_with("Phone number", secret=False)
        self.assertIn("With the country code, such as +44", "\n".join(self.lines()))


class ApiTest(EngineCase):
    """The engine API of redesign B1: what `flnd/__init__.py` exports, `Report`, and `download`'s options."""

    def engine(self, *sources):
        return Engine(home=self.home, sources={source.code: source for source in sources})

    def saved(self):
        with paths.table(self.home / paths.TARGETS, "targets") as targets:
            return dict(targets.items())

    def test_flnd_exports_exactly_the_engine_api(self):
        import flnd

        self.assertEqual(sorted(flnd.__all__), [
            "AuthInfo", "Engine", "Error", "LoginError", "Range",
            "Report", "SOURCES", "Source", "Target", "TargetError", "TargetInfo", "TargetReport",
            "__version__", "download", "open", "resolve", "source",
        ])
        self.assertFalse(hasattr(Engine, "LoginError"))
        self.assertFalse(hasattr(Engine, "run"))
        self.assertFalse(hasattr(Engine(home=self.home), "failed"))

    def test_report_files_failed_and_ok(self):
        good, bad = Target("aaa", "user", "good"), Target("aaa", "user", "bad")
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
                raise RuntimeError("blocked")
            return [File(f"{CDN}/{site.target.value}/a.jpg", name="a.jpg"), File(f"{CDN}/{site.target.value}/b.jpg",
                                                                                  name="b.jpg")]

        engine = self.engine(make_source("aaa", files=files))
        report = engine.download(["aaa:one", "aaa:bad"], categories="all")
        one, bad = report.targets
        self.assertEqual((one.target, bad.target), (Target("aaa", "user", "one"), Target("aaa", "user", "bad")))
        self.assertEqual(sorted(path.name for path in one.written), ["a.jpg", "b.jpg"])
        self.assertTrue(all(path.is_file() for path in one.written))
        self.assertEqual((one.skipped, one.error, bad.error), (0, None, "blocked"))
        self.assertEqual((report.files, report.failed, report.ok), (2, (Target("aaa", "user", "bad"),), False))
        again = engine.download([Target("aaa", "user", "one")], categories="all")
        self.assertEqual((again.files, again.targets[0].skipped, again.ok), (0, 2, True))

    def test_a_category_that_lists_to_its_end_is_recorded_complete(self):
        def files(site, context):
            if site.target.value == "cut":
                raise RuntimeError("cut short")
            return []

        self.engine(make_source("aaa", files=files)).download(["aaa:whole", "aaa:cut"], categories="all")
        rows = self.saved()
        self.assertEqual(rows["aaa:whole"]["complete"], {"posts": datetime.date.today().isoformat()})
        self.assertNotIn("complete", rows["aaa:cut"])
        # Re-running keeps the record.
        self.engine(make_source("aaa")).download(["aaa:whole"], categories="all")
        self.assertIn("complete", self.saved()["aaa:whole"])

    def test_text_and_values_are_read_alike(self):
        runs = []

        def run(engine, chosen, *, stop=None, tallies=(), browser_pool=None):
            runs.extend(options for _, options in chosen)
            return [TargetReport(target, (), 0) for target, _ in chosen]

        engine = self.engine(make_source("aaa"))
        with mock.patch.object(Engine, "_run", run):
            engine.download(["aaa:a"], categories="posts", time="2026-01-01,2026-06-30")
            engine.download(["aaa:b"], categories=["posts"], time=Range(datetime.date(2026, 1, 1),
                                                                        datetime.date(2026, 6, 30)))
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(runs[0], Options(categories=("posts",), since=datetime.date(2026, 1, 1),
                                          until=datetime.date(2026, 6, 30)))

    def test_bad_values_are_value_errors_before_anything_starts(self):
        from flnd.source import TIME_USAGE

        for text in ("2026-01-01", "2026-13-01,", "2026-06-30,2026-01-01", "yesterday,", "2026-01-01,2026-02-01,"):
            with self.subTest(time=text), self.assertRaisesRegex(ValueError, "^--time takes <since>,<until>"):
                Range.parse(text)
        self.assertEqual(Range.parse("*,2026-06-30"), Range(None, datetime.date(2026, 6, 30)))
        self.assertEqual(Range.parse("2026-01-01,"), Range(datetime.date(2026, 1, 1), None))
        engine = self.engine(make_source("aaa"))
        cases: tuple[tuple[dict[str, Any], str], ...] = (
            ({"time": "2026-01-01"}, TIME_USAGE),
            ({"categories": "reels"}, "no source here has 'reels'"),
        )
        for options, message in cases:
            with self.subTest(options=options), self.assertRaises(ValueError) as caught:
                engine.download(["aaa:a"], **options)
            self.assertTrue(str(caught.exception).startswith(message), caught.exception)
        with self.assertRaisesRegex(ValueError, "unknown source 'zzz'"):
            engine.download(["zzz:a"])
        self.assertFalse((self.home / paths.MEDIA).exists())
        self.assertFalse((self.home / paths.METADATA).exists())

    def test_from_with_a_post_link_is_refused_before_any_request(self):
        from flnd.sources import SOURCES

        message = ("--from applies to profiles and chats; ins:C1abcDEFghi is one post, downloaded whole: "
                   "give it without --from")
        engine = Engine(home=self.home, sources=SOURCES)
        for targets in (["https://www.instagram.com/p/C1abcDEFghi/"],
                        ["ins:example_user", "https://www.instagram.com/p/C1abcDEFghi/"],
                        ["tik:example_creator", "https://www.instagram.com/p/C1abcDEFghi/"]):
            with self.subTest(targets=targets), self.assertRaisesRegex(ValueError, f"^{message}$"):
                engine.download(targets, categories="all")
        self.assertEqual(self.requests, [])
        self.assertFalse((self.home / paths.MEDIA).exists())
        self.assertFalse((self.home / paths.METADATA).exists())
        # Without --from the same link downloads whole.
        runs = []
        def run(engine, chosen, *, stop=None, tallies=(), browser_pool=None):
            runs.extend(options for _, options in chosen)
            return [TargetReport(target, (), 0) for target, _ in chosen]

        with mock.patch.object(Engine, "_run", run):
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
        self.assertEqual(target.target, Target("aaa", "user", "a"))
        self.assertEqual(report.ok, True)


class ApiLoginTest(EngineCase):
    """`login` and `logout`: every credential in the vault, and nothing else written (redesign B7)."""

    def setUp(self):
        super().setUp()
        self.source = make_source("aaa", needs=("sid",))
        self.engine = Engine(home=self.home, sources={"aaa": self.source, "bbb": make_source("bbb")})
        jar = cookies.Jar([{"name": "sid", "value": "s", "domain": ".aaa.example", "path": "/"},
                           {"name": "other", "value": "o", "domain": ".example.com", "path": "/"}])
        patcher = mock.patch("shared.cookies.load", lambda browser="chrome", *, profile=None: jar)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_browser_login_lives_only_in_the_vault(self):
        self.assertEqual(self.engine.cookie_logins, ("aaa", "bbb"))
        self.assertEqual(self.engine.login("aaa", "browser", browser="chrome"), "aaa: logged in from Chrome (1 cookie)")
        self.assertEqual(sorted(path.name for path in self.home.rglob("*") if path.is_file()), ["secret.key", "secrets.db"])
        self.assertEqual(self.engine.logins, {"aaa": "logged in"})
        self.assertEqual({cookie["name"] for cookie in auth.load(self.home, "aaa")}, {"sid"})

    def test_a_file_login_reads_the_file_once_and_never_copies_it(self):
        path = self.home.parent / "cookies.txt"
        path.write_text("# Netscape HTTP Cookie File\n.aaa.example\tTRUE\t/\tTRUE\t1825861327\tsid\tv\n")
        self.assertEqual(self.engine.login("aaa", "file", path=path), "aaa: logged in from cookies.txt (1 cookie)")
        self.assertTrue(path.is_file())
        self.assertEqual(sorted(path.name for path in self.home.rglob("*") if path.is_file()), ["secret.key", "secrets.db"])

    def test_a_failed_login_and_bad_answers(self):
        with mock.patch("shared.cookies.load", lambda browser="chrome", *, profile=None: cookies.Jar()):
            with self.assertRaisesRegex(LoginError, "no sid for aaa.example in Chrome"):
                self.engine.login("aaa", "browser", browser="chrome")
        for args, options in ((("aaa", "qr"), {}), (("zzz", "browser"), {}), (("aaa", "browser"), {}),
                              (("aaa", "file"), {})):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.engine.login(*args, **options)
        self.assertEqual(self.engine.logins, {})

    def test_logout_clears_the_jar_and_returns_the_codes_that_had_one(self):
        self.engine.login("aaa", "browser", browser="chrome")
        self.assertEqual(self.engine.logout(["bbb", "aaa", "aaa"]), ["aaa"])
        self.assertEqual(self.engine.logins, {})
        self.assertEqual(auth.load(self.home, "aaa"), cookies.Jar())
        self.engine.login("aaa", "browser", browser="chrome")
        self.assertEqual(self.engine.logout(["all"]), ["aaa"])
        self.assertEqual(self.engine.logout(["all"]), [])
        with self.assertRaisesRegex(ValueError, "unknown source 'zzz'"):
            self.engine.logout(["zzz"])

    def test_a_download_uses_the_vaulted_login(self):
        self.engine.login("aaa", "browser", browser="chrome")
        seen = []
        self.engine = Engine(home=self.home, sources={"aaa": make_source("aaa", lambda site: seen.append(dict(site.cookies)),
                                                                         needs=("sid",))})
        self.assertTrue(self.engine.download(["aaa:a"], categories="all").ok)
        self.assertEqual(seen, [{"sid": "s"}])
        self.assertFalse(any("not logged in" in line for line in self.lines()))


class QueryAndLimitDryTest(EngineCase):
    """Query targets, target-yielding routes, `--limit`, `--dry`, and early close."""

    def _query_source(self, *, closed: list[bool]):
        class Searchable(Source):
            code, name, domains = "srch", "Searchable", ("srch.example",)

            @target("{id:\\d+}")
            def post(self, site, id):
                return id

            @post.category
            def photo(self, site, id):
                yield Post(id, [File(f"{CDN}/{id}.jpg", name=f"{id}.jpg")])

            @post.category
            def extra(self, site, id):
                yield Post(id, [File(f"{CDN}/{id}_extra.jpg", name=f"{id}_extra.jpg")])

            @target("list/{name}")
            def listing(self, site, name):
                yield Target("srch", "post", "1")
                yield f"srch:2"

            @target("?{query}", from_=["photo"])
            def query(self, site, query: str):
                try:
                    if "action" in query:
                        yield Target("srch", "post", "1")
                        yield Target("srch", "post", "2")
                        yield Target("srch", "post", "3")
                    if "comedy" in query:
                        yield Target("srch", "post", "2")
                        yield Target("srch", "post", "4")
                finally:
                    closed.append(True)

        return Searchable

    def test_query_route_dedupes_and_respects_from(self):
        closed = []
        src = self._query_source(closed=closed)
        self.routes[f"{CDN}/1.jpg"] = httpx.Response(200, content=b"1")
        self.routes[f"{CDN}/2.jpg"] = httpx.Response(200, content=b"2")
        self.routes[f"{CDN}/3.jpg"] = httpx.Response(200, content=b"3")
        self.routes[f"{CDN}/4.jpg"] = httpx.Response(200, content=b"4")
        engine = Engine(home=self.home, sources={"srch": src})

        report = engine.download(["srch?action comedy"], categories="photo", limit=4)
        self.assertTrue(report.ok)
        self.assertEqual([r.target.value for r in report.targets], ["1", "2", "3", "4"])
        self.assertEqual(len(closed), 1)
        self.assertFalse((self.target_dir("srch", "1") / "extra").exists())

    def test_limit_and_stop_close_generator_early(self):
        closed = []
        src = self._query_source(closed=closed)
        self.routes[f"{CDN}/1.jpg"] = httpx.Response(200, content=b"1")
        engine = Engine(home=self.home, sources={"srch": src})

        # --limit 1 on query route stops after 1 target and closes the generator.
        report = engine.download(["srch?action"], categories="photo", limit=1)
        self.assertEqual(len(report.targets), 1)
        self.assertEqual(closed, [True])

    def test_again_preserves_flag_for_normal_and_query_targets(self):
        seen_options: list[Options] = []
        src = self._query_source(closed=[])
        self.routes[f"{CDN}/1.jpg"] = httpx.Response(200, content=b"1")
        engine = Engine(home=self.home, sources={"srch": src})
        orig_download = job.download

        def spy(run, target, board, options):
            seen_options.append(options)
            return orig_download(run, target, board, options)

        with mock.patch("flnd.job.download", side_effect=spy):
            engine.download(["srch:1", "srch?action"], categories="photo", limit=1, again=True)
        self.assertTrue(seen_options)
        self.assertTrue(all(opt.again for opt in seen_options))

    def test_dry_run_on_query_and_normal_target_writes_nothing(self):
        closed = []
        src = self._query_source(closed=closed)
        engine = Engine(home=self.home, sources={"srch": src})

        report = engine.download(["srch?action"], categories="photo", limit=2, dry=True)
        self.assertTrue(report.ok)
        output = "\n".join(self.lines())
        self.assertIn("srch:1", output)
        self.assertIn("srch:2", output)
        self.assertFalse((self.home / paths.TARGETS).exists())
        self.assertFalse((self.home / paths.HISTORY).exists())
        self.assertFalse((self.home / paths.MEDIA).exists())

        report_normal = engine.download(["srch:1"], categories="photo", dry=True)
        self.assertTrue(report_normal.ok)
        self.assertIn("1.jpg", "\n".join(self.lines()))
        self.assertFalse((self.home / paths.MEDIA).exists())

    def test_limit_on_normal_target_and_target_yielding_route(self):
        class Multi(Source):
            code, name, domains = "mlt", "Multi", ("mlt.example",)

            @target("{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                for n in range(1, 5):
                    yield Post(str(n), [File(f"{CDN}/{n}.jpg", name=f"{n}.jpg")])

        for n in range(1, 5):
            self.routes[f"{CDN}/{n}.jpg"] = httpx.Response(200, content=str(n).encode())
        engine = Engine(home=self.home, sources={"mlt": Multi})
        report = engine.download(["mlt:example_user"], categories="all", limit=2)
        self.assertTrue(report.ok)
        self.assertEqual(len(report.targets[0].written), 2)

        closed = []
        src = self._query_source(closed=closed)
        engine2 = Engine(home=self.home, sources={"srch": src})
        report2 = engine2.download(["srch:list/all"], limit=1)
        self.assertTrue(report2.ok)
        self.assertTrue((self.target_dir("srch", "1") / "photo" / "1.jpg").is_file())
        self.assertFalse((self.target_dir("srch", "2") / "photo" / "2.jpg").exists())


class SyncedQueryTest(EngineCase):
    """Synced queries: 25 cap, known targets marked seen, `--dry`, Ctrl-C / `--limit` close, failing route."""

    def _paged_source(self, pages, *, closed, seen_pages, fail_on=None):
        class Paged(Source):
            code, name, domains = "pgd", "Paged", ("pgd.example",)

            @target("{id:\\d+}")
            def post(self, site, id):
                return id

            @post.category
            def photo(self, site, id):
                yield Post(id, [File(f"{CDN}/{id}.jpg", name=f"{id}.jpg")])

            @target("?{query}", from_=["photo"])
            def query(self, site, query: str):
                try:
                    for page_num, ids in enumerate(pages):
                        seen_pages.append(page_num)
                        if fail_on is not None and query == fail_on:
                            raise RuntimeError("search failed")
                        for item_id in ids:
                            yield Target("pgd", "post", str(item_id))
                finally:
                    closed.append(True)

        return Paged

    def test_30_new_results_start_25_and_leave_5_for_next_sync(self):
        pages = [list(range(1, 16)), list(range(16, 31))]
        closed, seen_pages = [], []
        src = self._paged_source(pages, closed=closed, seen_pages=seen_pages)
        for n in range(1, 31):
            self.routes[f"{CDN}/{n}.jpg"] = httpx.Response(200, content=str(n).encode())
        engine = Engine(home=self.home, sources={"pgd": src})
        # Save the query as synced with 0 initial results by patching pages for initial download,
        # or download --sync with limit=0? No, download --sync with 1st item, or toggle after dry?
        # Even simpler: record the synced query via download --sync on an empty page, then sync with 30 results!
        empty_src = self._paged_source([], closed=[], seen_pages=[])
        Engine(home=self.home, sources={"pgd": empty_src}).download(
            ["pgd?cats"], categories="photo", sync=True
        )

        report1 = engine.sync()
        self.assertTrue(report1.ok)
        self.assertEqual(len(report1.targets[0].written), 25)
        self.assertEqual(closed, [True])
        from flnd import targets as targets_db
        rows = targets_db.rows(self.home)
        self.assertEqual(len(targets_db.seen(rows, Target("pgd", "query", "cats"))), 25)

        closed.clear()
        seen_pages.clear()
        pages[:] = [list(range(31, 36)), *pages]
        for n in range(31, 36):
            self.routes[f"{CDN}/{n}.jpg"] = httpx.Response(200, content=str(n).encode())
        report2 = engine.sync()
        self.assertTrue(report2.ok)
        self.assertEqual(len(report2.targets[0].written), 5)
        # Stopped at pgd:1 on page 1 without asking for page 2.
        self.assertEqual(seen_pages, [0, 1])
        rows = targets_db.rows(self.home)
        self.assertEqual(len(targets_db.seen(rows, Target("pgd", "query", "cats"))), 30)

    def test_already_known_target_is_marked_seen_and_not_touched(self):
        from flnd import targets as targets_db
        pages = [[1, 2]]
        closed, seen_pages = [], []
        src = self._paged_source(pages, closed=closed, seen_pages=seen_pages)
        self.routes[f"{CDN}/1.jpg"] = httpx.Response(200, content=b"1")
        self.routes[f"{CDN}/2.jpg"] = httpx.Response(200, content=b"2")
        engine = Engine(home=self.home, sources={"pgd": src})
        # User already downloaded pgd:1 (not synced).
        engine.download(["pgd:1"], categories="photo")
        empty_src = self._paged_source([], closed=[], seen_pages=[])
        Engine(home=self.home, sources={"pgd": empty_src}).download(
            ["pgd?cats"], categories="photo", sync=True
        )

        self.requests.clear()
        report = engine.sync()
        self.assertTrue(report.ok)
        # Only pgd:2 was downloaded; pgd:1 was not touched and was marked seen.
        self.assertEqual([p.name for p in report.targets[0].written], ["2.jpg"])
        rows = targets_db.rows(self.home)
        self.assertEqual(targets_db.seen(rows, Target("pgd", "query", "cats")), ("pgd:1", "pgd:2"))

    def test_dry_lists_targets_and_marks_no_result_seen(self):
        pages = [[1, 2]]
        closed, seen_pages = [], []
        src = self._paged_source(pages, closed=closed, seen_pages=seen_pages)
        engine = Engine(home=self.home, sources={"pgd": src})
        report = engine.download(["pgd?cats"], categories="photo", dry=True, sync=True)
        self.assertTrue(report.ok)
        self.assertFalse((self.home / paths.TARGETS).exists())

    def test_ctrl_c_and_limit_close_generator_and_keep_partial_seen(self):
        from flnd import targets as targets_db
        pages = [[1], [2], [3]]
        closed, seen_pages = [], []
        src = self._paged_source(pages, closed=closed, seen_pages=seen_pages)
        self.routes[f"{CDN}/1.jpg"] = httpx.Response(200, content=b"1")
        self.routes[f"{CDN}/2.jpg"] = httpx.Response(200, content=b"2")
        engine = Engine(home=self.home, sources={"pgd": src})
        # --limit 1 stops after page 0 and closes the generator without asking for page 1.
        report = engine.download(["pgd?cats"], categories="photo", limit=1, sync=True)
        self.assertTrue(report.ok)
        self.assertEqual(seen_pages, [0])
        self.assertEqual(closed, [True])
        rows = targets_db.rows(self.home)
        self.assertEqual(targets_db.seen(rows, Target("pgd", "query", "cats")), ("pgd:1",))

        # Ctrl-C during item 2 stops before page 2 and keeps item 1 seen.
        closed.clear()
        seen_pages.clear()
        real_download = job.download
        stop_ref: list[cli.Stopping] = []
        real_stopping = cli.stopping

        @contextmanager
        def capture_stopping():
            with real_stopping() as stop:
                stop_ref.append(stop)
                yield stop

        def stop_on_second(run, child, board, opts):
            if child.value == "2":
                stop_ref[0].soon.set()
            return real_download(run, child, board, opts)

        with mock.patch("flnd.job.download", stop_on_second), \
                mock.patch.object(cli, "stopping", capture_stopping), \
                self.assertRaises(KeyboardInterrupt):
            engine.download(["pgd?dogs"], categories="photo", sync=True)
        self.assertEqual(seen_pages, [0, 1])
        self.assertEqual(closed, [True])
        rows = targets_db.rows(self.home)
        self.assertEqual(targets_db.seen(rows, Target("pgd", "query", "dogs")), ("pgd:1",))

    def test_failing_route_fails_target_and_next_target_still_runs(self):
        pages = [[1]]
        closed, seen_pages = [], []
        src = self._paged_source(pages, closed=closed, seen_pages=seen_pages, fail_on="bad")
        self.routes[f"{CDN}/1.jpg"] = httpx.Response(200, content=b"1")
        empty_src = self._paged_source([], closed=[], seen_pages=[])
        Engine(home=self.home, sources={"pgd": empty_src}).download(
            ["pgd?bad", "pgd?good"], categories="photo", sync=True
        )

        engine = Engine(home=self.home, sources={"pgd": src})
        report = engine.sync()
        self.assertFalse(report.ok)
        self.assertEqual([str(t) for t in report.failed], ["pgd?bad"])
        self.assertEqual([p.name for p in report.targets[1].written], ["1.jpg"])


if __name__ == "__main__":
    unittest.main()
