"""The engine end to end on httpx.MockTransport, with test sources. No network, no browser."""

import asyncio
import inspect
import io
import signal
import threading
import time
import unittest
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import httpx

from media_samples import needs_pillow

from flnd import engine as engine_module
from flnd.common import auth, net
from flnd.engine import Engine
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
    """

    def __init__(self, path, compress, check):
        self.path, self._compress, self._check = Path(path), compress, check
        self.size = self.path.stat().st_size

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
        engine = Engine(home=self.home, sources={source.code: source for source in sources}, **settings)
        return engine.run(engine.resolve(targets), options)

    def log_in(self, code, **values):
        jar = [{"name": name, "value": value, "domain": f".{code}.example", "path": "/"} for name, value in values.items()]
        cookies.Jar(jar).save(auth.cookie_file(self.home, code))

    def lines(self):
        return self.screen.dest.getvalue().splitlines()

    def target_dir(self, code, name):
        return self.home / "downloads" / code / name


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

        engine = Engine(home=self.home, sources={"aaa": make_source("aaa", connect=connect), "bbb": make_source("bbb")})
        engine.run(engine.resolve(["aaa:a", "aaa:b", "bbb:c"]), Options())
        lines = self.lines()
        self.assertIn("✗ aaa: tlg: the saved login no longer works", lines)
        self.assertFalse(any(line.startswith("✓ aaa:") for line in lines), lines)
        self.assertEqual(sum(line.startswith("✗ aaa:a") or line.startswith("✗ aaa:b") for line in lines), 2)
        self.assertIn("✓ bbb:c", lines)
        self.assertEqual([str(target) for target in engine.failed], ["aaa:a", "aaa:b"])

    def test_a_group_whose_logged_in_raises_ends_its_rows_failed(self):
        def logged_in(self, site):
            raise RuntimeError("can't read the login")

        engine = Engine(home=self.home, sources={"aaa": make_source("aaa", logged_in=logged_in)})
        engine.run(engine.resolve(["aaa:a"]), Options())
        self.assertFalse(any(line.startswith("✓ aaa:a") for line in self.lines()), self.lines())
        self.assertEqual([str(target) for target in engine.failed], ["aaa:a"])

    def test_failed_lists_only_the_targets_that_failed(self):
        def run(site):
            if site.target.value == "bad":
                raise RuntimeError("blocked")

        engine = Engine(home=self.home, sources={"aaa": make_source("aaa", run)})
        engine.run(engine.resolve(["aaa:good"]), Options())
        self.assertEqual(engine.failed, [])
        engine.run(engine.resolve(["aaa:bad", "aaa:good"]), Options())
        # It adds up over every run of one engine, as flnd sync runs one per settings group.
        self.assertEqual([str(target) for target in engine.failed], ["aaa:bad"])


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
        def limited(request):
            net.stopping.set()
            return httpx.Response(429)

        self.routes["https://api.aaa.example/feed"] = limited
        self.addCleanup(net.stopping.clear)
        with self.assertRaises(KeyboardInterrupt):
            self.run_engine(make_source("aaa", lambda site: site.fetch("https://api.aaa.example/feed").json),
                            targets=["aaa:a"])


class CtrlCTest(unittest.TestCase):
    def test_ctrl_c_sets_stopping_and_still_interrupts(self):
        before = signal.getsignal(signal.SIGINT)
        self.addCleanup(net.stopping.clear)
        with engine_module._stop_waits_on_ctrl_c():
            self.assertFalse(net.stopping.is_set())
            with self.assertRaises(KeyboardInterrupt):
                signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            self.assertTrue(net.stopping.is_set())
        self.assertIs(signal.getsignal(signal.SIGINT), before)


def history(home):
    with kv.open(home / "history.db", table="downloads") as rows:
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
        Engine(home=self.home, sources={"aaa": source}).login(source(), "qr")
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
        engine.login(source(), "qr")
        self.screen._interactive = True
        engine.login(source(), "qr")
        self.run_engine(source, targets=["aaa:a"])
        self.assertEqual(seen, [False, True, False])

    def test_ask_shows_its_description_in_the_question(self):
        answers = []

        def own_login(self, login, method):
            answers.append(login.ask("Phone number", description="With the country code, such as +44"))

        self.screen._interactive = True
        self.screen.readkey = iter(["1", "enter"]).__next__
        source = make_source("aaa", login=own_login, login_methods=("qr",))
        Engine(home=self.home, sources={"aaa": source}).login(source(), "qr")
        self.assertEqual(answers, ["1"])
        self.assertEqual(self.screen.dest.getvalue().splitlines()[:3],
                         ["┃ Phone number", "┃ With the country code, such as +44", "┃ > _"])


if __name__ == "__main__":
    unittest.main()
