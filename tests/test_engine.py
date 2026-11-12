"""The engine and its session, end to end on httpx.MockTransport, with test sources. No network, no browser."""

import asyncio
import io
import signal
import threading
import time
import unittest
import zipfile
from contextlib import asynccontextmanager, contextmanager
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import httpx

from media_samples import encoders, needs_ffmpeg, needs_pillow, sample

from flnd import engine as engine_module
from flnd.common import auth, net
from flnd.common.compress import Choice
from flnd.common.target import Target
from flnd.engine import Engine
from flnd.session import Options, TargetSession
from flnd.sources.base import FetchError, Source
from shared import cookies, kv, media, theme
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


def make_source(code, run, **declared):
    """A test source: `code:<name>` resolves to that user, and `run(session)` is the given function."""
    def resolve(self, text):
        return Target(code, "user", text.lstrip("@"))

    attributes = {"code": code, "name": code.upper(), "domains": (f"{code}.example",), "examples": (f"{code}:a",),
                  "categories": ("posts", "reels", "files"),
                  "resolve": resolve, "run": lambda self, session: run(session), **declared}
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

        def run(session):
            code = session.target.source
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
        def run(session):
            if session.target.value == "bad":
                raise RuntimeError("blocked")
            session.save(f"{CDN}/{session.target.value}.jpg", "a.jpg", category="posts")

        total = self.run_engine(make_source("aaa", run), targets=["aaa:bad", "aaa:good"])
        self.assertEqual(total, 1)
        lines = self.lines()
        self.assertIn("✗ aaa:bad: blocked", lines)
        self.assertIn("✗ aaa:bad", lines)
        self.assertIn("✓ aaa:good", lines)


    def test_a_group_whose_connect_fails_ends_its_rows_failed(self):
        # Fix 5: before it, only the error line was written and log.group ended the rows at ok.
        def connect(self, session):
            raise RuntimeError("tlg: the saved login no longer works")

        engine = Engine(home=self.home, sources={"aaa": make_source("aaa", lambda session: None, connect=connect),
                                                 "bbb": make_source("bbb", lambda session: None)})
        engine.run(engine.resolve(["aaa:a", "aaa:b", "bbb:c"]), Options())
        lines = self.lines()
        self.assertIn("✗ aaa: tlg: the saved login no longer works", lines)
        self.assertFalse(any(line.startswith("✓ aaa:") for line in lines), lines)
        self.assertEqual(sum(line.startswith("✗ aaa:a") or line.startswith("✗ aaa:b") for line in lines), 2)
        self.assertIn("✓ bbb:c", lines)
        self.assertEqual([str(target) for target in engine.failed], ["aaa:a", "aaa:b"])

    def test_a_group_whose_logged_in_raises_ends_its_rows_failed(self):
        def logged_in(self, session):
            raise RuntimeError("can't read the login")

        engine = Engine(home=self.home, sources={"aaa": make_source("aaa", lambda session: None, logged_in=logged_in)})
        engine.run(engine.resolve(["aaa:a"]), Options())
        self.assertFalse(any(line.startswith("✓ aaa:a") for line in self.lines()), self.lines())
        self.assertEqual([str(target) for target in engine.failed], ["aaa:a"])

    def test_failed_lists_only_the_targets_that_failed(self):
        def run(session):
            if session.target.value == "bad":
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
        source = make_source("aaa", lambda session: seen.append((dict(session.cookies),
                                                                 session.fetch_json("https://api.aaa.example/me"))),
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
        errors = []

        def run(session):
            try:
                session.ask("Phone number")
            except RuntimeError as error:
                errors.append(str(error))

        self.run_engine(make_source("aaa", run), targets=["aaa:a"])
        self.assertEqual(errors, ["aaa: flnd download never asks questions; run flnd login first"])


class HttpTest(EngineCase):
    def test_one_client_per_group_with_headers_user_agent_and_signing(self):
        clients = []

        def sign(self, request):
            request.headers["x-signed"] = "yes"
            return request

        def run(session):
            clients.append(session.http)
            session.fetch_json("https://api.aaa.example/a", headers={"x-extra": "1"})

        source = make_source("aaa", run, user_agent="Agent/1", sign=sign,
                             headers=lambda self, jar: {"x-app": "7"})
        self.routes["https://api.aaa.example/a"] = httpx.Response(200, json={})
        self.run_engine(source, targets=["aaa:a", "aaa:b"])
        self.assertIs(clients[0], clients[1])
        request = self.requests[0]
        self.assertEqual((request.headers["user-agent"], request.headers["x-app"], request.headers["x-signed"],
                          request.headers["x-extra"]), ("Agent/1", "7", "yes", "1"))

    def test_fetch_errors_and_soft(self):
        results = []

        def run(session):
            results.append(session.fetch_json("https://api.aaa.example/gone", soft=True))
            for url in ("https://api.aaa.example/gone", "https://api.aaa.example/html"):
                try:
                    session.fetch_json(url)
                except FetchError as error:
                    results.append((error.status, str(error)[:40]))
            results.append(session.fetch_html("https://api.aaa.example/html").css("h1").text)

        self.routes["https://api.aaa.example/gone"] = httpx.Response(404)
        self.routes["https://api.aaa.example/html"] = httpx.Response(200, html="<h1>Hi</h1>")
        self.run_engine(make_source("aaa", run), targets=["aaa:a"])
        self.assertEqual(results, [None, (404, "HTTP 404 for https://api.aaa.example/gon"),
                                   (None, "expected JSON from https://api.aaa.examp"), "Hi"])

    def test_a_rate_limit_never_stops_the_source(self):
        ran = []
        answers = [httpx.Response(429)] * 4 + [httpx.Response(200, json={"ok": 1})]

        def run(session):
            ran.append((session.target.value, session.fetch_json("https://api.aaa.example/feed")))

        self.routes["https://api.aaa.example/feed"] = lambda request: answers.pop(0) if len(answers) > 1 else answers[0]
        self.run_engine(make_source("aaa", run), targets=["aaa:a", "aaa:b", "aaa:c"])
        self.assertEqual(ran, [("a", {"ok": 1}), ("b", {"ok": 1}), ("c", {"ok": 1})])
        self.assertIn("! aaa: rate limited; waiting", " ".join(self.lines()))
        self.assertIn("(attempt 4)", " ".join(self.lines()))
        self.assertIn("✓ aaa:c", self.lines())

    def test_challenges_are_waited_out(self):
        self.run_engine(make_source("aaa", lambda session: session.challenged()),
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
            self.run_engine(make_source("aaa", lambda session: session.fetch_json("https://api.aaa.example/feed")),
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


class SaveTest(EngineCase):
    def test_paths_history_and_skipping(self):
        def run(session):
            session.folder = "posts"
            session.save(f"{CDN}/a.jpg", "2026-01-01_a.jpg", category="posts")
            session.save(f"{CDN}/a.jpg", "2026-01-01_a.jpg", category="posts")
            session.save(f"{CDN}/b.mp4", "../../escape.mp4", category="posts", folder="../videos")

        self.assertEqual(self.run_engine(make_source("aaa", run), targets=["aaa:someone"]), 2)
        folder = self.target_dir("aaa", "someone")
        self.assertEqual((folder / "posts" / "2026-01-01_a.jpg").read_bytes(), b"/a.jpg")
        # A name from a site never leaves the target's folder.
        self.assertTrue((folder / "untitled" / "videos" / "escape.mp4").is_file())
        self.assertIn("• skip posts/2026-01-01_a.jpg", self.lines())
        with kv.open(self.home / "history.db", table="downloads") as history:
            rows = dict(history.items())
        self.assertEqual(sorted(rows), ["aaa:posts/2026-01-01_a.jpg", "aaa:untitled/videos/escape.mp4"])
        self.assertEqual(rows["aaa:posts/2026-01-01_a.jpg"]["size"], 6)

        # A second run skips by the history row, keyed by `key`, wherever the file now is.
        self.requests.clear()
        self.assertEqual(self.run_engine(make_source("aaa", run), targets=["aaa:someone"]), 0)
        self.assertEqual(self.requests, [])

    def test_key_dedupes_across_folders(self):
        def run(session):
            self.assertTrue(session.save(f"{CDN}/p.jpg", "1_0.jpg", category="posts", folder="posts", key="1_0"))
            self.assertFalse(session.save(f"{CDN}/p.jpg", "1_0.jpg", category="posts", folder="images", key="1_0"))

        self.assertEqual(self.run_engine(make_source("aaa", run), targets=["aaa:a"]), 1)

    def test_kind_since_until_and_wants(self):
        answers = []

        def run(session):
            answers.append(session.wants(date=date(2025, 12, 31)))
            answers.append((session.categories, session.since, session.until))
            for name, category, day in (("old.mp4", "reels", date(2025, 12, 31)), ("in.mp4", "reels", date(2026, 3, 1)),
                                        ("late.mp4", "reels", date(2026, 7, 1)), ("post.jpg", "posts", date(2026, 3, 1)),
                                        ("undated.mp4", "reels", None)):
                answers.append(session.save(f"{CDN}/{name}", name, category=category, date=day))
            try:
                session.save(f"{CDN}/x.jpg", "x.jpg", category="liked")
            except ValueError as error:
                answers.append(str(error))

        options = Options(categories=("reels", "media"), since=date(2026, 1, 1), until=date(2026, 6, 30))
        self.run_engine(make_source("aaa", run), targets=["aaa:a"], options=options)
        self.assertEqual(answers, [False, (("reels",), date(2026, 1, 1), date(2026, 6, 30)),
                                   False, True, False, False, True,
                                   "aaa: unknown category 'liked'; declare it in categories (posts, reels, files)"])

    def test_categories_default_all_and_fallback(self):
        seen = []
        source = make_source("aaa", lambda session: seen.append(session.categories))
        for categories in (None, ("all",), ("files",), ("media",)):
            self.run_engine(source, targets=["aaa:a"], options=Options(categories=categories))
        # Without --from every category; a name the source doesn't have is ignored, and with none of
        # its own it takes every category, as without --from.
        every = ("posts", "reels", "files")
        self.assertEqual(seen, [every, every, ("files",), every])

    def test_no_limit_and_folder_none(self):
        # flnd has no download limit: every file a source saves is downloaded (simplify.md).
        added = []

        def run(session):
            for number in range(4):
                added.append(session.save(f"{CDN}/{number}.jpg", f"{number}.jpg", category="posts"))
            session.folder = None
            added.append(session.save(f"{CDN}/late.jpg", "late.jpg", category="posts"))
            added.append(session.write("note.md", "x"))

        self.assertEqual(self.run_engine(make_source("aaa", run), targets=["aaa:a"]), 4)
        self.assertEqual(added, [True, True, True, True, False, False])
        self.assertFalse(hasattr(TargetSession, "full"))
        self.assertFalse(any("limit" in line for line in self.lines()))

    def test_write_mirrors_and_fetch(self):
        def fetch(dest, progress):
            dest.write_bytes(b"fetched")
            progress(7, 7)

        def run(session):
            session.folder = "post"
            session.write("info.md", "# Title\n")
            session.save([f"{CDN}/dead.7z", f"{CDN}/f.7z"], "f.7z", category="files")
            session.save(fetch, "m.mp4", category="posts", size=7)

        self.routes[f"{CDN}/dead.7z"] = httpx.Response(404)
        self.run_engine(make_source("aaa", run), targets=["aaa:a"], options=Options(categories=("posts", "files")))
        folder = self.target_dir("aaa", "a") / "post"
        self.assertEqual((folder / "info.md").read_text(), "# Title\n")
        self.assertEqual((folder / "f.7z").read_bytes(), b"/f.7z")
        self.assertEqual((folder / "m.mp4").read_bytes(), b"fetched")
        self.assertIn(f"! mirror 1 failed: HTTP 404 for {CDN}/dead.7z; trying next", self.lines())

    def test_write_temp_file_is_named_part(self):
        """Fix 13: `write`'s temporary file is a `.<name>.<random>.part`, which `flnd sync` repair removes."""
        import fnmatch
        import os

        from flnd.common import files

        real_replace, moved = os.replace, []

        def spy(src, dst):
            moved.append(Path(src).name)
            return real_replace(src, dst)

        def run(session):
            session.folder = "post"
            session.write("info.md", "# Title\n")

        with mock.patch.object(os, "replace", spy):
            self.run_engine(make_source("aaa", run), targets=["aaa:a"])
        [name] = [name for name in moved if name.startswith(".info.md.")]
        self.assertTrue(fnmatch.fnmatch(name, files.PART_GLOB), name)
        self.assertEqual((self.target_dir("aaa", "a") / "post" / "info.md").read_bytes(), b"# Title\n")

    def test_failed_downloads_are_named(self):
        def run(session):
            session.save(f"{CDN}/broken.jpg", "broken.jpg", category="posts")

        self.routes[f"{CDN}/broken.jpg"] = lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused"))
        self.assertEqual(self.run_engine(make_source("aaa", run), targets=["aaa:a"]), 0)
        self.assertIn("✗ broken.jpg: ConnectError refused", self.lines())

    def test_queued_downloads_are_dropped_and_logged_when_run_fails(self):
        gate = threading.Event()

        def slow(request):
            gate.wait(5)
            return httpx.Response(200, content=b"x")

        def run(session):
            # One download runs (held by the gate) and two wait in the queue when run fails.
            for number in range(3):
                session.save(f"{CDN}/{number}.jpg", f"{number}.jpg", category="posts")
            threading.Timer(0.2, gate.set).start()
            raise RuntimeError("page closed")

        for number in range(3):
            self.routes[f"{CDN}/{number}.jpg"] = slow
        self.assertEqual(self.run_engine(make_source("aaa", run, concurrency=1), targets=["aaa:a"]), 1)
        self.assertIn("✗ aaa:a: page closed", self.lines())
        self.assertIn("! dropped 2 queued tasks after RuntimeError", self.lines())


class ArchiveTest(EngineCase):
    def test_an_archive_is_saved_as_sent_and_skipped_next_time(self):
        # --unpack and --zip are gone: flnd never opens an archive, and makes none of the folder.
        packed = io.BytesIO()
        with zipfile.ZipFile(packed, "w") as archive:
            archive.writestr("inside.txt", "hello")
        self.routes[f"{CDN}/set.zip"] = httpx.Response(200, content=packed.getvalue())
        source = make_source("aaa", lambda session: session.save(f"{CDN}/set.zip", "set.zip", category="files",
                                                                 folder="post"))
        for _ in range(2):
            self.run_engine(source, targets=["aaa:a"], options=Options(categories=("files",)))
        folder = self.target_dir("aaa", "a")
        self.assertEqual([path.name for path in (folder / "post").iterdir()], ["set.zip"])
        self.assertEqual((folder / "post" / "set.zip").read_bytes(), packed.getvalue())
        self.assertFalse(folder.with_name("a.zip").exists())
        self.assertEqual(sum(request.url.host == "cdn.example" for request in self.requests), 1)


def history(home):
    with kv.open(home / "history.db", table="downloads") as rows:
        return dict(rows.items())


@needs_pillow
class MediaSaveTest(EngineCase):
    def serve_file(self, name, data):
        self.routes[f"{CDN}/{name}"] = httpx.Response(200, content=data)

    def saver(self, *names, declared=None, **extra):
        def run(session):
            for name in names:
                session.save(f"{CDN}/{name}", name, category="posts", **extra)

        return make_source("aaa", run, **(declared or {}))

    def files(self):
        return sorted(path.name for path in self.target_dir("aaa", "a").rglob("*") if path.is_file())

    def test_a_webp_named_jpg_is_saved_as_webp_and_skipped_next_time(self):
        self.serve_file("a.jpg", noise("WEBP"))
        source = self.saver("a.jpg")
        self.assertEqual(self.run_engine(source, targets=["aaa:a"]), 1)
        self.assertEqual(self.files(), ["a.webp"])
        self.assertTrue(history(self.home)["aaa:a.jpg"]["path"].endswith("a.webp"))
        self.requests.clear()
        self.assertEqual(self.run_engine(source, targets=["aaa:a"]), 0)
        self.assertEqual(self.requests, [])

    def test_audio_is_for_videos_only(self):
        errors = []

        def run(session):
            try:
                session.save(f"{CDN}/a.jpg", "a.jpg", category="posts", audio=f"{CDN}/a.m4a")
            except RuntimeError as error:
                errors.append(str(error))

        self.run_engine(make_source("aaa", run), targets=["aaa:a"])
        self.assertEqual(errors, ["aaa: audio= is for videos, not a.jpg"])

    def test_without_ffmpeg_dash_videos_fail_with_medias_own_error(self):
        # No hint of flnd's own: ffmpeg comes with a required dependency, and media's error names it.
        missing = RuntimeError("shared.media needs ffmpeg for videos and audio: install ffmpeg on your PATH")
        with mock.patch("shared.media.open", side_effect=missing):
            self.run_engine(self.saver("1.mp4", "2.mp4", audio=f"{CDN}/a.m4a"), targets=["aaa:a"])
        self.assertFalse(any("ffmpeg not found" in line for line in self.lines()))
        self.assertEqual(sum("✗ 1.mp4: RuntimeError shared.media needs ffmpeg" in line
                             or "✗ 2.mp4: RuntimeError shared.media needs ffmpeg" in line for line in self.lines()), 2)
        self.assertEqual(self.files(), [])

    def test_image_replaces_the_photo_and_records_it(self):
        served = sample("big.jpg").read_bytes()
        self.serve_file("big.jpg", served)
        self.serve_file("v.mp4", b"\0\0\0\x18ftypisom" + bytes(64))
        options = Options(image=Choice(None, "low"))
        self.run_engine(self.saver("big.jpg", "v.mp4"), targets=["aaa:a"], options=options)
        self.assertEqual(self.files(), ["big.jpg", "v.mp4"])
        saved = self.target_dir("aaa", "a") / "big.jpg"
        self.assertLess(saved.stat().st_size, len(served))
        media.open(saved).check()
        self.assertEqual(media.open(saved).width, 2048)
        row = history(self.home)["aaa:big.jpg"]
        self.assertEqual((row["compressed"], row["size"], row["original_size"]),
                         ("low", saved.stat().st_size, len(served)))
        # --image leaves videos alone, and their rows say nothing of compressing.
        self.assertNotIn("compressed", history(self.home)["aaa:v.mp4"])
        self.requests.clear()
        self.run_engine(self.saver("big.jpg"), targets=["aaa:a"], options=Options(image=Choice(None, "high")))
        self.assertEqual(self.requests, [])

    def test_an_image_format_and_a_photo_that_doesnt_shrink(self):
        self.serve_file("a.jpg", noise("WEBP", (600, 400)))
        self.serve_file("tiny.png", sample("small.png").read_bytes())
        self.run_engine(self.saver("a.jpg", "tiny.png"), targets=["aaa:a"],
                        options=Options(image=Choice("jpg", "mid")))
        rows = history(self.home)
        self.assertEqual(self.files(), ["a.jpg", "tiny.jpg"] if rows["aaa:tiny.png"]["compressed"] != "none"
                         else ["a.jpg", "tiny.png"])
        self.assertEqual(media.open(self.target_dir("aaa", "a") / "a.jpg").format, "jpeg")
        self.assertTrue(rows["aaa:a.jpg"]["path"].endswith("a.jpg"))

    def test_audio_is_a_third_kind_named_by_its_content_and_compressed_with_audio(self):
        calls = []

        def compress(path, **settings):
            calls.append((Path(path).name, settings["level"], settings["format"]))
            return None

        self.serve_file("song.mp3", b"ID3" + bytes(64))
        self.serve_file("voice.mp3", b"\0\0\0\x18ftypM4A " + bytes(64))
        self.serve_file("a.jpg", noise("JPEG"))
        # media.check would need these to be real audio; the kind and the naming are what's tested here.
        with compressing(compress, check=False):
            self.run_engine(self.saver("song.mp3", "voice.mp3", "a.jpg"), targets=["aaa:a"],
                            options=Options(audio=Choice("opus", "mid")))
        self.assertEqual(sorted(calls), [("song.mp3", "mid", "opus"), ("voice.m4a", "mid", "opus")])
        self.assertEqual(self.files(), ["a.jpg", "song.mp3", "voice.m4a"])
        rows = history(self.home)
        self.assertEqual(rows["aaa:song.mp3"]["compressed"], "none")
        self.assertNotIn("compressed", rows["aaa:a.jpg"])

    def test_a_photo_that_cant_shrink_is_recorded_as_none(self):
        self.serve_file("small.png", sample("small.png").read_bytes())
        self.run_engine(self.saver("small.png"), targets=["aaa:a"], options=Options(image=Choice(None, "high")))
        row = history(self.home)["aaa:small.png"]
        self.assertEqual((row["compressed"], row["size"]), ("none", row["original_size"]))

    def test_a_broken_photo_is_kept_and_recorded_as_none(self):
        self.serve_file("a.jpg", noise("JPEG"))
        with compressing(side_effect=media.BrokenError("a.jpg: corrupt")):
            self.run_engine(self.saver("a.jpg"), targets=["aaa:a"], options=Options(image=Choice(None, "low")))
        self.assertEqual(history(self.home)["aaa:a.jpg"]["compressed"], "none")
        self.assertIn("! a.jpg: not compressed: a.jpg: corrupt", self.lines())

    def test_videos_are_compressed_one_at_a_time_and_the_target_waits(self):
        running, overlaps, done = [0], [], []

        def compress(path, **settings):
            running[0] += 1
            overlaps.append(running[0])
            time.sleep(0.05)
            running[0] -= 1
            out = Path(path).with_suffix("." + (settings["format"] or "mp4"))
            Path(path).unlink()
            out.write_bytes(b"small")
            done.append(out.name)
            return out

        for number in range(3):
            self.serve_file(f"{number}.webm", b"\x1a\x45\xdf\xa3webm" + bytes(64))
        with compressing(compress):
            self.run_engine(self.saver("0.webm", "1.webm", "2.webm"), targets=["aaa:a"],
                            options=Options(video=Choice("mp4", "low")))
        self.assertEqual(max(overlaps), 1)
        # The target's run returned only once every encode was done.
        self.assertEqual(sorted(done), ["0.mp4", "1.mp4", "2.mp4"])
        self.assertEqual(self.files(), ["0.mp4", "1.mp4", "2.mp4"])
        row = history(self.home)["aaa:0.webm"]
        self.assertEqual((row["compressed"], row["size"], Path(row["path"]).name), ("mp4,low", 5, "0.mp4"))

    def test_a_missing_encoder_stops_video_compression_and_photos_go_on(self):
        calls = []

        def compress(path, **settings):
            calls.append(Path(path).name)
            if Path(path).suffix == ".mp4":
                raise RuntimeError("this ffmpeg has no libx264 encoder")
            return None

        self.serve_file("1.mp4", b"\0\0\0\x18ftypisom" + bytes(64))
        self.serve_file("2.mp4", b"\0\0\0\x18ftypisom" + bytes(64))
        self.serve_file("a.jpg", noise("JPEG"))
        with compressing(compress):
            self.run_engine(self.saver("1.mp4", "2.mp4", "a.jpg", declared={"concurrency": 1}), targets=["aaa:a"],
                            options=Options(image=Choice(None, "low"), video=Choice(None, "low")))
        self.assertEqual(sorted(calls), ["1.mp4", "a.jpg"])
        self.assertEqual(sum("video compression stops" in line for line in self.lines()), 1)
        rows = history(self.home)
        self.assertNotIn("compressed", rows["aaa:1.mp4"])
        self.assertEqual(rows["aaa:a.jpg"]["compressed"], "none")

    def test_ctrl_c_during_an_encode_keeps_the_original_and_no_compressed_row(self):
        self.serve_file("v.mp4", b"\0\0\0\x18ftypisom" + bytes(64))
        with compressing(side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.run_engine(self.saver("v.mp4"), targets=["aaa:a"], options=Options(video=Choice(None, "low")))
        self.assertEqual(self.files(), ["v.mp4"])
        self.assertNotIn("compressed", history(self.home)["aaa:v.mp4"])

    @needs_ffmpeg("libx264")
    def test_video_shrinks_a_real_video(self):
        served = sample("big.mp4").read_bytes()
        self.serve_file("big.mp4", served)
        self.run_engine(self.saver("big.mp4"), targets=["aaa:a"], options=Options(video=Choice("h265", "low"))
                        if "libx265" in encoders() else Options(video=Choice(None, "low")))
        saved = self.target_dir("aaa", "a") / "big.mp4"
        self.assertLess(saved.stat().st_size, len(served))
        media.open(saved).check()
        self.assertEqual(media.open(saved).codec, "hevc" if "libx265" in encoders() else "h264")
        self.assertEqual(history(self.home)["aaa:big.mp4"]["compressed"], "h265,low" if "libx265" in encoders()
                         else "low")


class FakeBrowser:
    """Stands in for `browser.open()`: records options, opened URLs and listeners; holds cookies."""

    def __init__(self, **options):
        self.options = options
        self.added = []
        self.urls = []
        self.closed = False
        self.cookies = cookies.Jar([{"name": "csrftoken", "value": "late", "domain": ".aaa.example", "path": "/"}])
        self.listeners = []

    def add_cookies(self, jar):
        self.added.extend(jar)

    def open(self):
        browser = self

        class Page:
            url = "about:blank"

            def goto(self, url, idle=False, timeout=30.0):
                browser.urls.append(url)
                return 200

            def on_response(self, match, callback, lines=False):
                browser.listeners.append(callback)
                return SimpleNamespace(stop=lambda: browser.listeners.remove(callback))

        return Page()

    def close(self):
        self.closed = True


class BrowserTest(EngineCase):
    def setUp(self):
        super().setUp()
        self.browsers = []

        def connect(**options):
            self.browsers.append(FakeBrowser(**options))
            return self.browsers[-1]

        patcher = mock.patch("shared._browser.open", connect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_connects_only_on_open_page_with_settings_and_cookies(self):
        self.log_in("aaa", sid="1")
        self.run_engine(make_source("aaa", lambda session: None), targets=["aaa:a"])
        self.assertEqual(self.browsers, [])

        seen = []

        def run(session):
            session.on_response("graphql", lambda session, data, response: seen.append(data))
            session.open_page("https://aaa.example/a")
            session.goto("https://aaa.example/a/tab")
            for listener in self.browsers[0].listeners:
                listener({"n": 1}, None)
            session.fetch_json("https://api.aaa.example/x")

        self.routes["https://api.aaa.example/x"] = httpx.Response(200, json={})
        source = make_source("aaa", run, viewport=(800, 600), browser_args=("--no-sandbox",), stealth=False)
        self.run_engine(source, targets=["aaa:a"])
        connected = self.browsers[0]
        # Visible (open's default, so no headless=), and with the computer's own language and
        # timezone: flnd passes no locale= or timezone=.
        self.assertEqual(connected.options, {"viewport": (800, 600), "args": ("--no-sandbox",), "stealth": False})
        self.assertEqual([cookie["name"] for cookie in connected.added], ["sid"])
        self.assertEqual(connected.added[0]["domain"], ".aaa.example")
        self.assertEqual(connected.urls, ["https://aaa.example/a", "https://aaa.example/a/tab"])
        self.assertEqual(seen, [{"n": 1}])
        self.assertTrue(connected.closed)
        # A cookie the site set in the browser reached the client.
        self.assertIn("csrftoken=late", self.requests[0].headers["cookie"])

    def test_page_before_open_page_raises(self):
        errors = []

        def run(session):
            try:
                session.page
            except RuntimeError as error:
                errors.append(str(error))

        self.run_engine(make_source("aaa", run), targets=["aaa:a"])
        self.assertEqual(errors, ["aaa: no page is open; call session.open_page(url) first"])

    def test_a_missing_chrome_fails_the_target_naming_chrome(self):
        def connect(**options):
            raise RuntimeError("flnd needs Google Chrome: install it from https://www.google.com/chrome/")

        with mock.patch("shared._browser.open", connect):
            self.run_engine(make_source("aaa", lambda session: session.open_page("https://aaa.example/a")),
                            targets=["aaa:a"])
        self.assertIn("✗ aaa:a: flnd needs Google Chrome: install it from https://www.google.com/chrome/", self.lines())


class ClientTest(EngineCase):
    def test_sync_connect_is_entered_once_per_group(self):
        events = []

        @contextmanager
        def connect(self, session):
            events.append("enter")
            yield "client"
            events.append("exit")

        source = make_source("aaa", lambda session: events.append(session.client), connect=connect)
        self.run_engine(source, targets=["aaa:a", "aaa:b"])
        self.assertEqual(events, ["enter", "client", "client", "exit"])

    def test_async_connect_run_and_fetch_share_one_loop(self):
        loops = []

        @asynccontextmanager
        async def connect(self, session):
            loops.append(asyncio.get_running_loop())
            yield "telethon"

        async def fetch(dest, progress):
            loops.append(asyncio.get_running_loop())
            dest.write_bytes(b"media")

        async def run(session):
            loops.append(asyncio.get_running_loop())
            await asyncio.sleep(0)
            session.save(fetch, f"{session.client}.bin", category="posts")

        source = make_source("aaa", None, connect=connect)
        source.run = lambda self, session: run(session)
        self.assertEqual(self.run_engine(source, targets=["aaa:a"]), 1)
        self.assertEqual(len(set(map(id, loops))), 1)
        self.assertEqual(len(loops), 3)
        self.assertEqual((self.target_dir("aaa", "a") / "telethon.bin").read_bytes(), b"media")

    def test_an_async_run_saves_more_files_than_the_pool_queues(self):
        # Each fetch needs the source's loop, so a save made on that loop must never wait for the pool.
        @asynccontextmanager
        async def connect(self, session):
            yield "client"

        async def fetch(dest, progress):
            dest.write_bytes(b"media")

        async def run(session):
            for number in range(20):
                session.save(fetch, f"{number}.bin", category="posts")

        source = make_source("aaa", None, connect=connect, concurrency=1)
        source.run = lambda self, session: run(session)
        finished = []
        runner = threading.Thread(target=lambda: finished.append(self.run_engine(source, targets=["aaa:a"])),
                                  daemon=True)
        runner.start()
        runner.join(timeout=10)
        self.assertEqual(finished, [20])


class EngineLoginTest(EngineCase):
    def test_a_source_with_its_own_login_gets_an_asking_session(self):
        seen = []

        def own_login(self, session, method):
            seen.append((method, session.target, isinstance(session, TargetSession)))
            session.secrets["session"] = "s"

        source = make_source("aaa", lambda session: None, login=own_login, login_methods=("qr",))
        Engine(home=self.home, sources={"aaa": source}).login(source(), "qr")
        self.assertEqual(seen, [("qr", Target("aaa", "account", ""), True)])
        self.assertEqual(str(seen[0][1]), "aaa")
        from flnd.common import auth
        with auth.secrets(self.home, "aaa") as secrets:
            self.assertEqual(secrets["session"], "s")

    def test_interactive_only_during_login_in_a_terminal(self):
        seen = []

        def own_login(self, session, method):
            seen.append(session.interactive)

        source = make_source("aaa", lambda session: seen.append(session.interactive), login=own_login,
                             login_methods=("qr",))
        engine = Engine(home=self.home, sources={"aaa": source})
        engine.login(source(), "qr")
        self.screen._interactive = True
        engine.login(source(), "qr")
        self.run_engine(source, targets=["aaa:a"])
        self.assertEqual(seen, [False, True, False])

    def test_ask_shows_its_description_in_the_question(self):
        answers = []

        def own_login(self, session, method):
            answers.append(session.ask("Phone number", description="With the country code, such as +44"))

        self.screen._interactive = True
        self.screen.readkey = iter(["1", "enter"]).__next__
        source = make_source("aaa", lambda session: None, login=own_login, login_methods=("qr",))
        Engine(home=self.home, sources={"aaa": source}).login(source(), "qr")
        self.assertEqual(answers, ["1"])
        self.assertEqual(self.screen.dest.getvalue().splitlines()[:3],
                         ["┃ Phone number", "┃ With the country code, such as +44", "┃ >"])


if __name__ == "__main__":
    unittest.main()
