"""src/engine/save.py: a post's files through the save's nine stages.

Each post runs through a real `flnkit.net.Client` on `net.fake`, a fake scheduler that keeps the
retry policy's rules, ffmpeg faked by `files.fake`, and `clock.Fake`, in a temporary home.
`RealSchedulerTest` runs the save on `flnd.engine.jobs` itself, once that module is in the tree.
"""

import asyncio
import builtins
import contextlib
import errno
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from unittest import mock

from flnd.engine import home, save
from flnd.engine.events import Failed, Saved, Skipped
from flnd.engine.save import Item, Kind, Saver, Store
from flnd.errors import DiskError, Error, NotFound, SiteError, ToolError, UsageError
from flnd.source import File, Target
from flnd.ui.log import ui
from flnkit import clock, files, net

try:
    from flnd.engine import jobs as real
except ImportError:
    real = None

T = Target("ins:example_user", kind="user")
CDN = "https://cdn.example.com"
PNG, JPEG, MP4 = b"\x89PNG\r\n\x1a\n" + b"p" * 8, b"\xff\xd8\xff" + b"j" * 8, b"\0\0\0\x18ftypisom"
DAY = date(2026, 5, 1)


class Jobs:
    """The scheduler, faked with the retry policy's rules: each job runs at once; a retryable
    error or a 5xx is tried again, 4 times at most; a `DiskError` (a busy part's too) or a
    missing tool stops the run, and every job after it raises it."""

    def __init__(self) -> None:
        self.kinds: list[Kind] = []
        self.stop: BaseException | None = None

    async def run(self, job):
        for tries in range(5):
            if self.stop is not None:
                raise self.stop
            self.kinds.append(job.kind)
            try:
                return await job.work()
            except Exception as error:
                missing = isinstance(error, ToolError) and not isinstance(error, files.RunError)
                if isinstance(error, DiskError) or missing:
                    self.stop = error
                    raise
                server = isinstance(error, SiteError) and (error.status or 0) >= 500
                if not (isinstance(error, Error) and error.retryable or server) or tries == 4:
                    raise


class SaveCase(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)
        self.lines = io.StringIO()
        self.enterContext(ui.scope(dest=self.lines, width=300, color=False))
        self.clock = self.enterContext(clock.use(clock.Fake()))
        self.routes: dict = {}
        self.enterContext(net.fake(lambda request: self.serve(request)))
        with home.Home(self.tmp) as made:
            made.clear("")  # a first write makes flnd.db with today's schema
        self.store = Store(self.tmp / home.DB)
        self.addCleanup(self.store.close)
        self.jobs = Jobs()
        self.events: list = []

    def serve(self, request):
        answer = self.routes[request.url]
        return answer(request) if callable(answer) else answer

    def item(self, name: str, url="", **fields) -> Item:
        file = fields.pop("file", None) or File(url or f"{CDN}/{name}")
        options = {"kind": files.IMAGE, "day": DAY, **fields}
        return Item(f"ins:{name}", options.pop("target", T), options.pop("post", "1"), "posts",
                    file, self.tmp / "media" / name, **options)

    def save(self, *items: Item, **options) -> Saver:
        saver = Saver(self.store, self.jobs, self.events.append, **options)

        async def post() -> None:
            await saver.post(items, client=net.Client(net.Session(), pace=False), plugin="ins")
            await saver.close()

        asyncio.run(post())
        return saver

    def rows(self, columns="key, path, size, date, hash") -> list[tuple]:
        return self.store._sql.execute(f"SELECT {columns} FROM files ORDER BY key").fetchall()

    def saved(self) -> list[Path]:
        return [event.path for event in self.events if isinstance(event, Saved)]


class StagesTest(SaveCase):
    def test_a_file_lands_named_by_its_content_dated_and_recorded(self) -> None:
        cases = [("a.jpg", PNG, files.IMAGE, "a.png"), ("b.jpeg", JPEG, files.IMAGE, "b.jpeg"),
                 ("c", MP4, files.SNIFF, "c.mp4"), ("d.bin", b"\x01\x02", None, "d.bin")]
        for name, body, kind, landed in cases:
            with self.subTest(name):
                self.routes[f"{CDN}/{name}"] = body
                self.save(self.item(name, kind=kind))
                path = self.tmp / "media" / landed
                self.assertEqual(path.read_bytes(), body)
                noon = datetime(2026, 5, 1, 12, tzinfo=UTC).timestamp()
                self.assertEqual(path.stat().st_mtime, noon)
                self.assertIn((f"ins:{name}", str(path), len(body), "2026-05-01", None),
                              self.rows())
                self.assertEqual(self.saved()[-1], path)
        self.assertEqual(list((self.tmp / "media").glob("*.part")), [])

    def test_a_web_page_fails_the_file_and_keeps_no_part(self) -> None:
        self.routes[f"{CDN}/a.jpg"] = b"<!doctype html><title>log in</title>"
        self.save(self.item("a.jpg"))
        [failed] = self.events
        self.assertIsInstance(failed, Failed)
        self.assertIn("got a web page instead of a photo", str(failed.error))
        self.assertEqual(os.listdir(self.tmp / "media"), [])
        self.assertEqual(self.store._sql.execute("SELECT key, class FROM failures").fetchall(),
                         [("ins:a.jpg", "SiteError")])

    def test_the_mirrors_are_tried_in_order(self) -> None:
        self.routes.update({f"{CDN}/one.jpg": 500, f"{CDN}/two.jpg": JPEG})
        self.save(self.item("a.jpg", file=File((f"{CDN}/one.jpg", f"{CDN}/two.jpg"))))
        self.assertEqual((self.tmp / "media" / "a.jpg").read_bytes(), JPEG)
        self.assertIn("mirror 1 failed", self.lines.getvalue())

    def test_a_part_another_run_holds_is_skipped_as_busy(self) -> None:
        self.routes[f"{CDN}/a.jpg"] = JPEG
        with files.part(self.tmp / "media" / "a.jpg", resume=True):
            self.save(self.item("a.jpg"))
        self.assertEqual(self.events, [Skipped(T, "posts", "1", self.tmp / "media" / "a.jpg",
                                               reason="busy")])
        self.assertIsNone(self.jobs.stop, "a busy part is the file's, never the run's stop")

    def test_a_cancel_while_the_part_opens_leaves_no_lock(self) -> None:
        opening, go, real_open = threading.Event(), threading.Event(), files._open
        self.routes[f"{CDN}/a.jpg"] = JPEG

        def slow(path, **options):
            opening.set()
            go.wait(5)
            return real_open(path, **options)

        async def cancelled() -> None:
            saver = Saver(self.store, self.jobs, self.events.append)
            client = net.Client(net.Session(), pace=False)
            task = asyncio.ensure_future(saver.post([self.item("a.jpg")], client=client,
                                                    plugin="ins"))
            await asyncio.to_thread(opening.wait, 5)
            task.cancel()
            go.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with mock.patch.object(files, "_open", slow):
            asyncio.run(cancelled())
        with files.part(self.tmp / "media" / "a.jpg", resume=True) as held:
            held.drop()

    def test_a_plugins_fetch_function_writes_the_part_and_stops_on_ctrl_c(self) -> None:
        def fetch(path, progress):
            Path(path).write_bytes(JPEG)
            progress(len(JPEG))

        self.save(self.item("a.jpg", file=File(fetch)))
        self.assertEqual((self.tmp / "media" / "a.jpg").read_bytes(), JPEG)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(KeyboardInterrupt):
            self.save(self.item("b.jpg", file=File(fetch)), stop=stop)
        self.assertEqual((self.tmp / "media" / "b.jpg.part").read_bytes(), JPEG)

    def test_an_expired_link_is_asked_for_again(self) -> None:
        self.routes.update({f"{CDN}/old.jpg": 403, f"{CDN}/new.jpg": JPEG})

        async def refresh() -> File:
            return File(f"{CDN}/new.jpg")

        self.save(self.item("a.jpg", url=f"{CDN}/old.jpg", refresh=refresh))
        self.assertEqual(self.saved(), [self.tmp / "media" / "a.jpg"])


class DashTest(SaveCase):
    def test_a_video_and_its_audio_are_joined(self) -> None:
        # (audio, ffmpeg fails the first try, what is saved, ffmpeg runs)
        cases = [("joined", b"A" * 9, False, b"merged", 1),
                 ("a failed join run again", b"A" * 9, True, b"merged", 2),
                 ("empty audio saves the video alone", b"", False, b"V" * 9, 0)]
        on_loop: list = []

        def spy(real):
            def opening(path, *args, **options):
                if threading.current_thread() is threading.main_thread():
                    on_loop.append(str(path))
                return real(path, *args, **options)
            return opening

        for label, audio, flaky, body, runs in cases:
            with self.subTest(label):
                name = f"{len(self.events)}.mp4"
                self.routes.update({f"{CDN}/v.mp4": b"V" * 9, f"{CDN}/a.mp4": audio})
                ran: list = []

                def ffmpeg(argv, flaky=flaky, ran=ran):
                    ran.append(argv)
                    if flaky and len(ran) == 1:
                        return 1, "", "Invalid data found when processing input in#0"
                    Path(argv[-1]).write_bytes(b"merged")
                    return 0, "", ""

                with files.fake({"ffmpeg": ffmpeg}), \
                        mock.patch.object(files, "_open", spy(files._open)), \
                        mock.patch.object(builtins, "open", spy(builtins.open)):
                    self.save(self.item(name, file=File(f"{CDN}/v.mp4", audio=f"{CDN}/a.mp4"),
                                        kind=None))
                self.assertEqual((self.tmp / "media" / name).read_bytes(), body)
                self.assertEqual(len(ran), runs)
                self.assertEqual(on_loop, [], "no part opens on the loop's thread")
                self.assertEqual(sorted(os.listdir(self.tmp / "media"))[-1], name)
        self.assertEqual([type(event) for event in self.events], [Saved] * 3)


class DedupeTest(SaveCase):
    def test_a_file_is_hashed_only_when_a_saved_one_has_its_size(self) -> None:
        # (saved file's bytes, new file's bytes, links work, linked)
        cases = [("no size match", b"xxxxx", JPEG, True, False),
                 ("same size, other bytes", b"\xff\xd8\xffzzzzzzzz", JPEG, True, False),
                 ("same bytes", JPEG, JPEG, True, True),
                 ("same bytes, no hard links", JPEG, JPEG, False, False)]
        for number, (label, old, new, links, linked) in enumerate(cases):
            with self.subTest(label):
                self.store._sql.execute("DELETE FROM files")
                held = self.tmp / f"held{number}.jpg"
                held.write_bytes(old)
                self.store.commit([home.FileRow(f"ins:held{number}", "ins:example_user",
                                                str(held), len(old))], [], {}, None)
                self.routes[f"{CDN}/{number}.jpg"] = new
                self.jobs.kinds.clear()
                link = files.link if links else (lambda *paths: False)
                with mock.patch.object(files, "link", link):
                    self.save(self.item(f"{number}.jpg"))
                path = self.tmp / "media" / f"{number}.jpg"
                self.assertEqual(path.read_bytes(), new)
                self.assertEqual(os.path.samefile(path, held), linked)
                hashes = dict(self.rows("key, hash"))
                sized = len(old) == len(new)
                self.assertEqual(self.jobs.kinds.count(Kind.HASH), 2 if sized else 0)
                self.assertEqual(hashes[f"ins:{number}.jpg"] is not None, sized)
                self.assertEqual(hashes[f"ins:held{number}"] is not None, sized)
        self.assertEqual(self.lines.getvalue().count("can't hard-link"), 1)


class CommitTest(SaveCase):
    def test_a_posts_rows_go_in_one_transaction(self) -> None:
        statements: list[str] = []
        self.store._sql.set_trace_callback(statements.append)
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            self.routes[f"{CDN}/{name}"] = JPEG
        self.save(self.item("a.jpg", count=3), self.item("b.jpg"), self.item("c.jpg"))
        self.assertEqual(statements.count("BEGIN IMMEDIATE"), 1)
        self.assertEqual(len(self.rows()), 3)
        self.assertEqual(self.store._sql.execute("PRAGMA busy_timeout").fetchone(), (0,))

    def test_a_write_lock_held_elsewhere_never_stalls_the_loop(self) -> None:
        other = sqlite3.connect(self.store.path, isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        slept, sleep = self.clock.slept, self.clock.sleep

        def pause(seconds, *, stop=None):
            if len(slept) == 2:
                other.execute("COMMIT")
            return sleep(seconds, stop=stop)

        stalls, commit = [], self.store.commit

        def timed(*args):
            start = time.perf_counter()
            try:
                return commit(*args)
            finally:
                stalls.append(time.perf_counter() - start)

        self.routes[f"{CDN}/a.jpg"] = JPEG
        with mock.patch.object(self.clock, "sleep", pause), \
                mock.patch.object(self.store, "commit", timed):
            self.save(self.item("a.jpg"))
        self.assertEqual(slept, [save.BUSY_PAUSE] * 3)
        self.assertLessEqual(max(stalls), 0.1)
        self.assertEqual([row[0] for row in self.rows()], ["ins:a.jpg"])
        other.close()

    def test_the_log_is_checkpointed_every_so_many_rows_and_at_close(self) -> None:
        self.routes.update({f"{CDN}/a.jpg": JPEG, f"{CDN}/b.jpg": PNG})
        with mock.patch.object(save, "CHECKPOINT_ROWS", 2), \
                mock.patch.object(Store, "checkpoint", autospec=True) as checkpoint:
            self.save(self.item("a.jpg"), self.item("b.jpg"))
        self.assertEqual(checkpoint.call_count, 2)


class StopTest(SaveCase):
    def test_what_stops_the_run_and_what_fails_one_file(self) -> None:
        def full(self, *data, **options):
            raise OSError(errno.ENOSPC, "No space left on device")

        def refused(*args):
            raise DiskError("flnd.db: database or disk is full")

        def dropped(request):
            raise ConnectionResetError("reset")

        # (answer, patch, video, what is raised, what the file's event is)
        cases = [("a full disk", JPEG, (files.Part, "write", full), False, DiskError, None),
                 ("a full disk on landing", JPEG, (files.Part, "flush", full), False, DiskError,
                  None),
                 ("a refused commit", JPEG, (self.store, "commit", refused), False, DiskError,
                  None),
                 ("no ffmpeg", JPEG, None, True, files.MissingError, None),
                 ("a dropped connection", dropped, None, False, None, Failed),
                 ("a missing file", 404, None, False, None, Failed)]
        for label, answer, patch, video, raised, event in cases:
            with self.subTest(label):
                self.events.clear()
                self.jobs = Jobs()
                name = f"{label}.jpg"
                self.routes.update({f"{CDN}/{name}": answer, f"{CDN}/a.mp4": JPEG})
                file = File(f"{CDN}/{name}", audio=f"{CDN}/a.mp4" if video else None)
                with mock.patch.object(*patch) if patch else contextlib.nullcontext():
                    if raised is None:
                        self.save(self.item(name, file=file, kind=None))
                    else:
                        with files.fake({}), self.assertRaises(raised):
                            self.save(self.item(name, file=file, kind=None))
                self.assertEqual([type(one) for one in self.events], [event] if event else [])
        gone = self.store._sql.execute("SELECT state FROM files WHERE key = ?",
                                       ("ins:a missing file.jpg",)).fetchone()
        self.assertEqual(gone, ("gone",))
        self.assertIsInstance(self.events[0].error, NotFound)


class FinishTest(SaveCase):
    def test_archives_of_two_targets_are_each_unpacked_and_saved(self) -> None:
        other = Target("ins:example_creator", kind="user")
        for name in ("a.zip", "b.zip"):
            body = io.BytesIO()
            with zipfile.ZipFile(body, "w") as archive:
                archive.writestr("inside.txt", name)
            self.routes[f"{CDN}/{name}"] = body.getvalue()
        self.save(self.item("a.zip", kind=None), self.item("b.zip", kind=None, target=other))
        self.assertEqual(sorted((event.path.name, event.target.value) for event in self.events),
                         [("a.zip", "example_user"), ("b.zip", "example_creator")])
        self.assertEqual({row[0] for row in self.rows("unpacked")}, {"extracted"})
        for folder, in self.rows("folder"):
            self.assertTrue((Path(folder) / "inside.txt").is_file())

    def test_a_sidecar_holds_the_posts_fields_and_its_row_the_posts_commit(self) -> None:
        statements: list[str] = []
        self.store._sql.set_trace_callback(statements.append)
        self.routes.update({f"{CDN}/a.jpg": JPEG, f"{CDN}/b.jpg": PNG})
        meta = {"post": "1", "caption": "hello", "day": DAY}
        self.save(self.item("a.jpg", meta=meta), self.item("b.jpg", meta=meta))
        side = self.tmp / "media" / "a.jpg.json"
        self.assertEqual(json.loads(side.read_text()),
                         {"post": "1", "caption": "hello", "day": "2026-05-01"})
        self.assertEqual(self.rows("sidecar"), [(str(side),), (str(side.with_name("b.png.json")),)])
        writes = [one.split()[0] for one in statements if one.startswith(("BEGIN", "UPDATE"))]
        self.assertEqual(writes, ["BEGIN"])

    def test_exec_runs_without_a_shell_and_a_failure_only_warns(self) -> None:
        record = self.tmp / "got.json"
        script = ("import json, os, sys; json.dump([sys.argv[1:], os.environ['FLND_POST']],"
                  f" open({str(record)!r}, 'w')); sys.exit(len(sys.argv) > 3)")
        name = "a b'; touch PWNED; $(touch PWNED2) {post}.jpg"
        self.routes[f"{CDN}/x.jpg"] = JPEG
        self.save(self.item(name, url=f"{CDN}/x.jpg"),
                  exec=[sys.executable, "-c", script, "{path}", "post={post}"])
        path = str(self.tmp / "media" / name)
        self.assertEqual(json.loads(record.read_text()), [[path, "post=1"], "1"])
        self.assertFalse(any(self.tmp.rglob("PWNED*")))
        self.assertEqual(self.saved(), [Path(path)])
        self.save(self.item(name, url=f"{CDN}/x.jpg"),
                  exec=[sys.executable, "-c", script, "1", "2", "3"])
        self.assertIn("exit 1", self.lines.getvalue())
        for wrong in ("echo {path}", [], ["echo", 1]):
            with self.subTest(wrong), self.assertRaises(UsageError):
                Saver(self.store, self.jobs, self.events.append, exec=wrong)


@unittest.skipIf(real is None, "flnd.engine.jobs isn't in this tree yet")
class RealSchedulerTest(SaveCase):
    """The save on the scheduler itself: its pools, its retry rule and what stops a run."""

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.object(save, "Kind", real.Kind))
        self.enterContext(mock.patch.object(save, "Job", real.Job))
        self.jobs = self.enterContext(real.Scheduler())

    def test_a_busy_part_a_flaky_join_and_a_sidecar(self) -> None:
        self.routes.update({f"{CDN}/a.jpg": JPEG, f"{CDN}/b.jpg": PNG,
                            f"{CDN}/v.mp4": b"V" * 9, f"{CDN}/au.mp4": b"A" * 9})
        ran: list = []

        def ffmpeg(argv):
            ran.append(argv)
            if len(ran) == 1:
                return 1, "", "Invalid data found when processing input in#0"
            Path(argv[-1]).write_bytes(b"merged")
            return 0, "", ""

        dash = File(f"{CDN}/v.mp4", audio=f"{CDN}/au.mp4")
        with files.part(self.tmp / "media" / "a.jpg", resume=True), \
                files.fake({"ffmpeg": ffmpeg}):
            self.save(self.item("a.jpg"), self.item("b.jpg", meta={"post": "1"}),
                      self.item("v.mp4", file=dash, kind=None))
        self.assertEqual(sorted(type(event).__name__ for event in self.events),
                         ["Saved", "Saved", "Skipped"])
        self.assertIsNone(self.jobs._stop)
        self.assertEqual(len(ran), 2)


if __name__ == "__main__":
    unittest.main()
