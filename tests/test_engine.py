"""The engine end to end on httpx.MockTransport, with test sources. No network, no browser.

Its runs, groups, HTTP, clients and logins through `Engine._run` (what `download` and `sync` call
per settings group), then the API itself (redesign B1): `download`, `sync`, `login`, `logout` and
`Report`, the options read from text, and `--from` refused for a post. The screen is a fake one,
and `shared.media.open` a `FakeMedia` where only what was compressed matters.
"""

import asyncio
import datetime
import inspect
import io
import signal
import threading
import time
import unittest
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import httpx

from media_samples import needs_pillow

from flnd import engine as engine_module
from flnd.common import auth, files, net
from flnd import Engine, LoginError, Range, Report, Target, TargetReport
from flnd.session import Options
from flnd.site import LoginSite
from flnd.source import FetchError, File, Post, Source, target
from shared import cookies, http, kv, media, theme
from shared._theme import Screen

CDN = "https://cdn.example"


def noise(format, size=(64, 48)):
    from PIL import Image

    out = io.BytesIO()
    Image.effect_noise(size, 40).convert("RGB").save(out, format)
    return out.getvalue()


class FakeMedia:
    """Stands in for `media.open(path)`'s `Media` in compress tests: `compress` calls the test's function.

    The function is `compress(path, **settings)`, with the level as `settings["level"]`. It returns
    None for "nothing smaller", or the path of a smaller file it wrote, which `save` then returns.
    `info.video` is `video_codec`, which a test sets to give a video its codec.
    """

    video_codec = None

    def __init__(self, path, compress, check):
        self.path, self._compress, self._check = Path(path), compress, check
        self.size = self.path.stat().st_size

    @property
    def info(self):
        return SimpleNamespace(video=self.video_codec)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def close(self):
        pass

    def check(self):
        if self._check:
            REAL_OPEN(self.path).check()
        return self

    def compress(self, level, **settings):
        out = self._compress(self.path, level=level, **settings)
        return self if out is None else FakeResult(Path(out))


class FakeResult:
    """What `FakeMedia.compress` returns for a smaller file: `size`, and `save` giving its path."""

    def __init__(self, path):
        self.path = path
        self.size = path.stat().st_size

    def save(self, dest=None):
        return self.path


REAL_OPEN = media.open


def compressing(compress=None, *, check=True, **options):
    """Patch `media.open` with `FakeMedia`, compressing with `compress` or a `mock.Mock(**options)`.

        with compressing(side_effect=TimeoutError("stalled")) as compress:
            ...
        compress.assert_called_once()

    With `check` (the default), `Media.check` still runs for real, as a download's check does.
    """
    fake = compress or mock.Mock(**options)

    @contextmanager
    def patched():
        with mock.patch("shared.media.open", lambda path: FakeMedia(path, fake, check)):
            yield fake

    return patched()


def make_source(code, route=None, *, files=(), **declared):
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
        found = files(site, context) if callable(files) else files
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
        self.screen = Screen(dest=io.StringIO(), inp=io.StringIO(""), width=400, tty=False, interactive=False)
        scope = theme.scope(screen=self.screen, color="none", unicode=True)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / ".flnd"
        self.requests = []
        self.routes = {}
        self.pauses = []
        self.now = 0.0
        patches = [
            mock.patch.object(net, "_base_transport", lambda: httpx.MockTransport(self.serve)),
            mock.patch.object(net, "_sleep", self.sleep),
            mock.patch.object(net, "_clock", lambda: self.now),
            # `http.download`'s waits between its own attempts.
            mock.patch.object(http, "_sleep", self.sleep),
            mock.patch.object(engine_module, "_sleep", self.pauses.append),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def sleep(self, seconds):
        self.now += seconds

    def serve(self, request):
        self.requests.append(request)
        answer = self.routes.get(str(request.url).split("?")[0])
        if callable(answer):
            return answer(request)
        if answer is None:
            return httpx.Response(200, content=request.url.path.encode())
        return answer

    def run_engine(self, *sources, targets, options=Options(), **settings):
        """Run the targets with one set of options, as `download` does per group, and return how many files were written."""
        return self.report(*sources, targets=targets, options=options, **settings).files

    def report(self, *sources, targets, options=Options(), **settings):
        """Run the targets with one set of options, as `download` does per group, and return the `Report`."""
        engine = Engine(home=self.home, sources={source.code: source for source in sources}, **settings)
        return Report(tuple(report for report, _ in engine._run(engine.resolve(targets), options)))

    def log_in(self, code, **values):
        jar = [{"name": name, "value": value, "domain": f".{code}.example", "path": "/"} for name, value in values.items()]
        auth.save(self.home, code, cookies.Jar(jar))

    def lines(self):
        return self.screen.dest.getvalue().splitlines()

    def target_dir(self, code, name):
        return self.home / files.MEDIA / code / name


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
        self.assertEqual((peaks["aaa"], peaks["bbb"]), (1, 1))
        self.assertGreater(peaks["all"], 1)
        # A pause before every target of a source but its first: two for aaa, one for bbb.
        self.assertEqual(len(self.pauses), 3)
        self.assertTrue(all(3.0 <= pause <= 7.0 for pause in self.pauses), self.pauses)

    def test_rows_end_at_ok_or_error_and_the_run_goes_on(self):
        def run(site):
            if site.target.value == "bad":
                raise RuntimeError("blocked")

        def files(site, context):
            return [File(f"{CDN}/{site.target.value}.jpg", name="a.jpg")]

        total = self.run_engine(make_source("aaa", run, files=files), targets=["aaa:bad", "aaa:good"])
        self.assertEqual(total, 1)
        lines = self.lines()
        self.assertIn("✗ aaa:bad: blocked", lines)
        self.assertIn("✗ aaa:bad", lines)
        self.assertIn("✓ aaa:good", lines)


    def test_a_group_whose_connect_fails_ends_its_rows_failed(self):
        # Fix 5: before it, only the error line was written and log.group ended the rows at ok.
        def connect(self, site):
            raise RuntimeError("tlg: the saved login no longer works")

        report = self.report(make_source("aaa", connect=connect), make_source("bbb"), targets=["aaa:a", "aaa:b", "bbb:c"])
        lines = self.lines()
        self.assertIn("✗ aaa: tlg: the saved login no longer works", lines)
        self.assertFalse(any(line.startswith("✓ aaa:") for line in lines), lines)
        self.assertEqual(sum(line.startswith("✗ aaa:a") or line.startswith("✗ aaa:b") for line in lines), 2)
        self.assertIn("✓ bbb:c", lines)
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
        self.assertIn("! aaa: not logged in, using public access; run flnd login", self.lines())

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
        self.assertIn("✗ aaa:a: not logged in; run flnd login", self.lines())
        self.assertIn("✗ aaa:b", self.lines())

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
            results.append(site.fetch("https://api.aaa.example/gone", soft=True))
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
        self.assertEqual(results, [None, (404, "HTTP 404 for https://api.aaa.example/gon"),
                                   (None, "expected JSON from https://api.aaa.examp"), "Hi"])

    def test_a_rate_limit_never_stops_the_source(self):
        ran = []
        answers = [httpx.Response(429)] * 4 + [httpx.Response(200, json={"ok": 1})]

        def run(site):
            ran.append((site.target.value, site.fetch("https://api.aaa.example/feed").json))

        self.routes["https://api.aaa.example/feed"] = lambda request: answers.pop(0) if len(answers) > 1 else answers[0]
        self.run_engine(make_source("aaa", run), targets=["aaa:a", "aaa:b", "aaa:c"])
        self.assertEqual(ran, [("a", {"ok": 1}), ("b", {"ok": 1}), ("c", {"ok": 1})])
        self.assertIn("! aaa: rate limited; waiting", " ".join(self.lines()))
        self.assertIn("(attempt 4)", " ".join(self.lines()))
        self.assertIn("✓ aaa:c", self.lines())

    def test_challenges_are_waited_out(self):
        self.run_engine(make_source("aaa", lambda site: site.back_off("challenged")),
                        targets=["aaa:a", "aaa:b", "aaa:c", "aaa:d"])
        waits = [line for line in self.lines() if "challenged; waiting" in line]
        self.assertEqual([wait.rsplit(" ", 1)[-1] for wait in waits], ["1)", "2)", "3)", "4)"])
        self.assertIn("✓ aaa:d", self.lines())

    def test_ctrl_c_during_a_wait_stops_the_run(self):
        """The first Ctrl-C ends a rate-limit wait at once; the target ends as stopped and the command as Ctrl-C."""
        def limited(request):
            net.stopping.set()
            return httpx.Response(429)

        self.routes["https://api.aaa.example/feed"] = limited
        self.addCleanup(net.stopping.clear)
        source = make_source("aaa", lambda site: site.fetch("https://api.aaa.example/feed").json)
        engine = Engine(home=self.home, sources={"aaa": source})
        with self.assertRaises(KeyboardInterrupt):
            engine.download(["aaa:a"])
        self.assertIn("✗ aaa:a stopped", self.lines())


class CtrlCTest(unittest.TestCase):
    def setUp(self):
        for event in (net.stopping, net.stopping_now):
            self.addCleanup(event.clear)

    def test_the_first_ctrl_c_stops_gracefully_and_the_second_interrupts(self):
        """D8: the first only sets `stopping`, so the running downloads finish; the second stops now."""
        before = signal.getsignal(signal.SIGINT)
        net.stopping.set()
        net.stopping_now.set()
        with engine_module._stop_waits_on_ctrl_c():
            # A run starts with neither set, whatever an earlier one left.
            self.assertFalse(net.stopping.is_set() or net.stopping_now.is_set())
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            self.assertTrue(net.stopping.is_set())
            self.assertFalse(net.stopping_now.is_set())
            with self.assertRaises(KeyboardInterrupt):
                signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            self.assertTrue(net.stopping_now.is_set())
        self.assertIs(signal.getsignal(signal.SIGINT), before)


def history(home):
    with kv.open(home / files.HISTORY, table="downloads") as rows:
        return dict(rows.items())


@needs_pillow
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
        engine.login("aaa", "qr")
        self.screen._interactive = True
        engine.login("aaa", "qr")
        self.run_engine(source, targets=["aaa:a"])
        self.assertEqual(seen, [False, True, False])

    def test_ask_shows_its_description_in_the_question(self):
        answers = []

        def own_login(self, login, method):
            answers.append(login.ask("Phone number", description="With the country code, such as +44"))

        self.screen._interactive = True
        self.screen.readkey = iter(["1", "enter"]).__next__
        source = make_source("aaa", login=own_login, login_methods=("qr",))
        Engine(home=self.home, sources={"aaa": source}).login("aaa", "qr")
        self.assertEqual(answers, ["1"])
        self.assertEqual(self.screen.dest.getvalue().splitlines()[:3],
                         ["┃ Phone number", "┃ With the country code, such as +44", "┃ > _"])


class ApiTest(EngineCase):
    """The engine API of redesign B1: what `flnd/__init__.py` exports, `Report`, and `download`'s options."""

    def engine(self, *sources):
        return Engine(home=self.home, sources={source.code: source for source in sources})

    def saved(self):
        with kv.open(self.home / files.TARGETS, table="targets") as targets:
            return dict(targets.items())

    def test_flnd_exports_exactly_the_engine_api(self):
        import flnd

        self.assertEqual(sorted(flnd.__all__), ["Engine", "LoginError", "Range", "Report", "Target", "TargetError",
                                                "TargetReport"])
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
        report = engine.download(["aaa:one", "aaa:bad"])
        one, bad = report.targets
        self.assertEqual((one.target, bad.target), (Target("aaa", "user", "one"), Target("aaa", "user", "bad")))
        self.assertEqual(sorted(path.name for path in one.written), ["a.jpg", "b.jpg"])
        self.assertTrue(all(path.is_file() for path in one.written))
        self.assertEqual((one.skipped, one.error, bad.error), (0, None, "blocked"))
        self.assertEqual((report.files, report.failed, report.ok), (2, (Target("aaa", "user", "bad"),), False))
        again = engine.download([Target("aaa", "user", "one")])
        self.assertEqual((again.files, again.targets[0].skipped, again.ok), (0, 2, True))

    def test_a_category_that_lists_to_its_end_is_recorded_complete(self):
        def files(site, context):
            if site.target.value == "cut":
                raise RuntimeError("cut short")
            return []

        self.engine(make_source("aaa", files=files)).download(["aaa:whole", "aaa:cut"])
        rows = self.saved()
        self.assertEqual(rows["aaa:whole"]["complete"], {"posts": datetime.date.today().isoformat()})
        self.assertNotIn("complete", rows["aaa:cut"])
        # A setting saved later keeps the record.
        self.engine(make_source("aaa")).download(["aaa:whole"], shrink=True)
        self.assertIn("complete", self.saved()["aaa:whole"])

    def test_text_and_values_are_read_alike(self):
        runs = []

        def run(engine, targets, options, sync=False):
            runs.append(options)
            return [(TargetReport(target, (), 0), ()) for target in targets]

        engine = self.engine(make_source("aaa"))
        with mock.patch.object(Engine, "_run", run):
            engine.download(["aaa:a"], categories="posts", time="2026-01-01,2026-06-30", shrink=True)
            engine.download(["aaa:b"], categories=["posts"], time=Range(datetime.date(2026, 1, 1),
                                                                        datetime.date(2026, 6, 30)),
                            shrink=True)
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(runs[0], Options(categories=("posts",), since=datetime.date(2026, 1, 1),
                                          until=datetime.date(2026, 6, 30), shrink=True))

    def test_off_turns_a_saved_setting_off_and_none_keeps_it(self):
        engine = self.engine(make_source("aaa"))
        report = engine.download(["aaa:a"], shrink=True)
        self.assertEqual(report.targets[0].settings, "")
        self.assertIs(self.saved()["aaa:a"]["shrink"], True)
        # None keeps the saved setting, and says it uses it.
        report = engine.download(["aaa:a"])
        self.assertEqual(report.targets[0].settings, "--shrink (saved)")
        self.assertIs(self.saved()["aaa:a"]["shrink"], True)
        # False turns it off, and nothing is shown as saved after that.
        report = engine.download(["aaa:a"], shrink=False)
        self.assertEqual(report.targets[0].settings, "")
        self.assertIs(self.saved()["aaa:a"]["shrink"], False)
        self.assertEqual(engine.download(["aaa:a"]).targets[0].settings, "")
        self.assertIs(self.saved()["aaa:a"]["shrink"], False)

    def test_bad_values_are_value_errors_before_anything_starts(self):
        from flnd.source import TIME_USAGE

        for text in ("2026-01-01", "2026-13-01,", "2026-06-30,2026-01-01", "yesterday,", "2026-01-01,2026-02-01,"):
            with self.subTest(time=text), self.assertRaisesRegex(ValueError, "^--time takes <since>,<until>"):
                Range.parse(text)
        self.assertEqual(Range.parse("*,2026-06-30"), Range(None, datetime.date(2026, 6, 30)))
        self.assertEqual(Range.parse("2026-01-01,"), Range(datetime.date(2026, 1, 1), None))
        engine = self.engine(make_source("aaa"))
        for options, message in (({"time": "2026-01-01"}, TIME_USAGE),
                                 ({"categories": "reels"}, "no source here has 'reels'")):
            with self.subTest(options=options), self.assertRaises(ValueError) as caught:
                engine.download(["aaa:a"], **options)
            self.assertTrue(str(caught.exception).startswith(message), caught.exception)
        with self.assertRaisesRegex(ValueError, "unknown source 'zzz'"):
            engine.download(["zzz:a"])
        self.assertFalse((self.home / files.MEDIA).exists())
        self.assertFalse((self.home / files.METADATA).exists())

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
        self.assertFalse((self.home / files.MEDIA).exists())
        self.assertFalse((self.home / files.METADATA).exists())
        # Without --from the same link downloads whole.
        runs = []
        with mock.patch.object(Engine, "_run", lambda engine, targets, options, sync=False: runs.append(options) or
                               [(TargetReport(target, (), 0), ()) for target in targets]):
            self.assertTrue(engine.download(["https://www.instagram.com/p/C1abcDEFghi/"]).ok)
        self.assertEqual(runs, [Options()])

    def test_sync_reports_repaired_and_missing(self):
        listed = {"a.jpg", "b.jpg", "c.jpg"}

        def files(site, context):
            return [File(f"{CDN}/{name}", name=name) for name in sorted(listed)]

        engine = self.engine(make_source("aaa", files=files))
        engine.download(["aaa:a"], shrink=True)
        folder = self.target_dir("aaa", "a")
        (folder / "a.jpg").unlink()
        (folder / "c.jpg").unlink()
        listed.discard("c.jpg")
        with compressing(return_value=None, check=False):
            report = engine.sync()
        [target] = report.targets
        self.assertEqual((target.target, target.repaired, target.missing), (Target("aaa", "user", "a"), 1, 1))
        self.assertEqual([path.name for path in target.written], ["a.jpg"])
        self.assertEqual((target.settings, report.ok), ("--shrink (saved)", True))
        self.assertIn("• aaa:a: 2 files, ", "\n".join(self.lines()))
        self.assertEqual(engine.sync([Target("aaa", "user", "a")]).targets[0].repaired, 0)


class ApiLoginTest(EngineCase):
    """`login` and `logout`: every credential in the vault, and nothing else written (redesign B7)."""

    def setUp(self):
        super().setUp()
        self.source = make_source("aaa", needs=("sid",))
        self.engine = Engine(home=self.home, sources={"aaa": self.source, "bbb": make_source("bbb")})
        jar = cookies.Jar([{"name": "sid", "value": "s", "domain": ".aaa.example", "path": "/"},
                           {"name": "other", "value": "o", "domain": ".example.com", "path": "/"}])
        patcher = mock.patch("shared._cookies.from_browser", lambda browser="chrome", *, profile=None: jar)
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
        with mock.patch("shared._cookies.from_browser", lambda browser="chrome", *, profile=None: cookies.Jar()):
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
        self.assertTrue(self.engine.download(["aaa:a"]).ok)
        self.assertEqual(seen, [{"sid": "s"}])
        self.assertFalse(any("not logged in" in line for line in self.lines()))


if __name__ == "__main__":
    unittest.main()
