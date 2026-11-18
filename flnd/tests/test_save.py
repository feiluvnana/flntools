"""src/engine/save.py: a post's files through the save's nine stages.

Each post runs through a real `flnkit.net.Client` on `net.fake`, the run's scheduler
(`flnd.engine.jobs`), ffmpeg faked by `files.fake`, and `clock.Fake`, in a temporary home.
`EngineTest` runs it inside the engine.
"""

import asyncio
import builtins
import contextlib
import errno
import hashlib
import io
import json
import os
import shutil
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

from fakes import EngineCase, make_source

from flnd.engine import home, jobs, save
from flnd.engine.events import Failed, Saved, Skipped
from flnd.engine.jobs import Kind
from flnd.engine.save import Item, Saver, Store
from flnd.errors import DiskError, NotFound, UsageError
from flnd.source import File, Target
from flnd.ui.log import ui
from flnkit import clock, files, net

T = Target("ins:example_user", kind="user")
CDN = "https://cdn.example.com"
PNG, JPEG, MP4 = b"\x89PNG\r\n\x1a\n" + b"p" * 8, b"\xff\xd8\xff" + b"j" * 8, b"\0\0\0\x18ftypisom"
DAY = date(2026, 5, 1)


class Jobs(jobs.Scheduler):
    """The run's scheduler, keeping the kind of each job it runs."""

    def __init__(self) -> None:
        super().__init__()
        self.kinds: list[Kind] = []

    async def run(self, job):
        self.kinds.append(job.kind)
        return await super().run(job)


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
        self.addCleanup(lambda: self.jobs.__exit__())
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
            await saver.post(items, client=net.Client(net.Session()), plugin="ins")
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
                self.assertIn((f"ins:{name}", str(path), len(body), "2026-05-01"),
                              self.rows("key, path, size, date"))
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
        self.assertIsNone(self.jobs._stop, "a busy part is the file's, never the run's stop")

    def test_a_cancel_while_the_part_opens_leaves_no_lock(self) -> None:
        opening, go, real_open = threading.Event(), threading.Event(), files._open
        self.routes[f"{CDN}/a.jpg"] = JPEG

        def slow(path, **options):
            opening.set()
            go.wait(5)
            return real_open(path, **options)

        async def cancelled() -> None:
            saver = Saver(self.store, self.jobs, self.events.append)
            client = net.Client(net.Session())
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


class SegmentTest(SaveCase):
    """6.2's segments: a big file from a host that takes ranges, on the fake clock, which each
    256 KiB chunk the server sends moves on by `pace` s."""

    def serve_ranges(self, body: bytes, *, ranges=True, etag='"v1"', pace=0.25,
                     log=None, stop=None):
        def answer(request):
            span = request.headers.get("range")
            if log is not None:
                log.append(span)
            start, end = 0, len(body)
            if ranges and span and request.headers.get("if-range") in (None, etag):
                first, _, last = span.removeprefix("bytes=").partition("-")
                start, end = int(first), int(last) + 1 if last else len(body)
            piece = body[start:end]

            def chunks():
                for at in range(0, len(piece), 256 << 10):
                    if stop is not None and stop():
                        raise ConnectionResetError("reset")
                    self.clock.advance(pace)
                    yield piece[at:at + (256 << 10)]

            headers = {"etag": etag, **({"accept-ranges": "bytes"} if ranges else {})}
            if start or end < len(body):
                headers["content-range"] = f"bytes {start}-{end - 1}/{len(body)}"
                return net.reply(206, headers=headers, chunks=chunks())
            return net.reply(200, headers={**headers, "content-length": str(len(body))},
                             chunks=chunks())
        return answer

    def test_when_a_file_splits_and_how_far(self) -> None:
        mib = 1 << 20
        # (file, its size, ranges, the most segments, at least)
        cases = [("small.bin", 8 * mib, True, 1, 1), ("plain.bin", 24 * mib, False, 1, 1),
                 ("big.bin", 24 * mib, True, 8, 2), ("huge.bin", 96 * mib, True, 8, 8)]
        for name, size, ranges, most, least in cases:
            with self.subTest(name):
                body = os.urandom(size)
                log: list = []
                self.routes[f"{CDN}/{name}"] = self.serve_ranges(body, ranges=ranges, log=log)
                self.save(self.item(name, kind=None))
                self.assertEqual((self.tmp / "media" / name).read_bytes(), body)
                starts = {span.removeprefix("bytes=").partition("-")[0] for span in log}
                self.assertTrue(least <= len(starts) <= most, log)
                self.assertFalse((self.tmp / "media" / f"{name}.part.json").exists())

    def test_a_mirror_serving_another_file_falls_back_to_the_first(self) -> None:
        body, log, other = os.urandom(24 << 20), [], []
        self.routes[f"{CDN}/one.bin"] = self.serve_ranges(body, log=log)
        self.routes[f"{CDN}/two.bin"] = self.serve_ranges(body, etag='"v2"', log=other)
        self.save(self.item("m.bin", file=File((f"{CDN}/one.bin", f"{CDN}/two.bin")),
                            kind=None))
        self.assertEqual((self.tmp / "media" / "m.bin").read_bytes(), body)
        self.assertTrue(other, "the second mirror was asked for a segment")
        self.assertGreater(len(log), 1, "its segment came from the first URL")

    def test_a_stopped_file_resumes_each_segment_from_its_sidecar(self) -> None:
        body, log, sent = os.urandom(32 << 20), [], []

        def stop():
            sent.append(1)
            return len(sent) > 70

        self.routes[f"{CDN}/r.bin"] = self.serve_ranges(body, log=log, stop=stop)
        with mock.patch.object(jobs, "NETWORK_WAITS", ()):
            self.save(self.item("r.bin", kind=None))
        kept = json.loads((self.tmp / "media" / "r.bin.part.json").read_text())
        self.assertGreater(len(kept["segments"]), 1)
        self.assertEqual((kept["size"], kept["etag"]), (len(body), '"v1"'))
        log.clear()
        sent.clear()
        self.routes[f"{CDN}/r.bin"] = self.serve_ranges(body, log=log)
        self.save(self.item("r.bin", kind=None))
        self.assertEqual((self.tmp / "media" / "r.bin").read_bytes(), body)
        # Each segment goes on from what it had written, never from 0.
        resumed = sorted(int(span.removeprefix("bytes=").partition("-")[0]) for span in log)
        self.assertEqual(resumed[:len(kept["segments"])],
                         sorted(start + written for start, _, written in kept["segments"]
                                if start + written < _)[:len(kept["segments"])])
        self.assertNotIn("bytes=0-", log)


    def test_a_crash_after_a_segment_ends_resumes_to_the_same_bytes(self) -> None:
        # Each sidecar counts only bytes on disk: a segment's last piece, under 1 MiB, never
        # waits in the part's buffer. A crash is what the disk holds as a sidecar is written.
        body = os.urandom((28 << 20) + 200_000)
        crash, claimed = self.tmp / "crash", []
        real_keep = files.Part.keep

        def keep(held, *args):
            real_keep(held, *args)
            segments = json.loads(held.side_path.read_bytes())["segments"]
            disk = Path(held.path).read_bytes()
            claimed.extend((start, written) for start, _, written in segments
                           if disk[start:start + written] != body[start:start + written])
            ended = [written == end - start for start, end, written in segments]
            if not crash.exists() and any(ended) and not all(ended):
                crash.mkdir()
                shutil.copy(held.path, crash / "c.bin.part")
                shutil.copy(held.side_path, crash / "c.bin.part.json")

        self.routes[f"{CDN}/c.bin"] = self.serve_ranges(body)
        with mock.patch.object(files.Part, "keep", keep):
            self.save(self.item("c.bin", kind=None))
        self.assertEqual(claimed, [], "a sidecar counted bytes that weren't on disk")
        self.assertTrue(crash.exists(), "no segment ended while another went on")
        (self.tmp / "media" / "c.bin").unlink()
        for name in ("c.bin.part", "c.bin.part.json"):
            shutil.copy(crash / name, self.tmp / "media" / name)
        self.save(self.item("c.bin", kind=None))
        self.assertEqual((self.tmp / "media" / "c.bin").read_bytes(), body)


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
                # A file is hashed only when a saved one has its size, and then both are.
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

    def test_a_commit_that_meets_a_lock_is_queued_again(self) -> None:
        other = sqlite3.connect(self.store.path, isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        tries, commit = [], self.store.commit

        def counted(*args):
            tries.append(self.jobs.kinds[-1])
            if len(tries) == 3:
                other.execute("COMMIT")
            return commit(*args)

        self.routes[f"{CDN}/a.jpg"] = JPEG
        with mock.patch.object(self.store, "commit", counted):
            self.save(self.item("a.jpg"))
        # The `WRITE` job, tried again at its priority, never a thread's wait.
        self.assertEqual(tries, [Kind.WRITE] * 3)
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


class EngineTest(EngineCase):
    """The save inside a run of the engine: what stops the run, and what each file ends as."""

    def run_files(self, *found: File, targets=("aaa:example_user",)):
        return self.report(make_source("aaa", files=list(found)), targets=list(targets))

    def saved(self) -> list[str]:
        return sorted(event.path.name for event in self.events if isinstance(event, Saved))

    def test_a_full_disk_or_a_refused_commit_stops_the_run(self) -> None:
        # E1 and E15: the run raises the `DiskError` (exit 4), never a file's failure alone.
        def full(*args, **options):
            raise OSError(errno.ENOSPC, "No space left on device")

        def refused(*args, **options):
            raise DiskError("flnd.db: database or disk is full")

        self.routes[f"{CDN}/a.jpg"] = JPEG
        for where, how in (((files.Part, "write"), full), (COMMIT, refused)):
            with self.subTest(where[1]), mock.patch.object(*where, how):
                with self.assertRaises(DiskError):
                    self.run_files(File(f"{CDN}/a.jpg", name="a.jpg"))

    def test_a_join_that_fails_once_is_tried_again_and_empty_audio_saves_the_video(self):
        # E6 and E7.
        self.routes.update({f"{CDN}/v.mp4": b"V" * 9, f"{CDN}/a.mp4": b"A" * 9,
                            f"{CDN}/w.mp4": b"W" * 9, f"{CDN}/silent.mp4": b""})
        ran: list = []

        def ffmpeg(argv):
            ran.append(argv)
            if len(ran) == 1:
                return 1, "", "Invalid data found when processing input in#0"
            Path(argv[-1]).write_bytes(b"merged")
            return 0, "", ""

        folder = self.target_dir("aaa", "example_user")
        for name, audio, body in (("v.mp4", "a.mp4", b"merged"), ("w.mp4", "silent.mp4", b"W" * 9)):
            with self.subTest(name), files.fake({"ffmpeg": ffmpeg}):
                self.events.clear()
                self.run_files(File(f"{CDN}/{name}", audio=f"{CDN}/{audio}", name=name))
                self.assertEqual(self.saved(), [name])
                self.assertEqual((folder / name).read_bytes(), body)

    def test_a_fetch_file_takes_its_plugins_first_domain_and_its_limit(self) -> None:
        def fetch(path, progress):
            Path(path).write_bytes(JPEG)
            progress(len(JPEG))

        caps: list = []
        real = jobs.Scheduler.run

        async def run(scheduler, job):
            if job.kind is Kind.FILE:
                caps.append((job.host, scheduler.limits.cap(job.host).most))
            return await real(scheduler, job)

        source = make_source("aaa", files=[File(fetch, name="a.jpg"), File(fetch, name="b.jpg")],
                             limits={"aaa.example": 1})
        with mock.patch.object(jobs.Scheduler, "run", run):
            self.report(source, targets=["aaa:example_user"])
        self.assertEqual(self.saved(), ["a.jpg", "b.jpg"])
        self.assertEqual(caps, [("aaa.example", 1)] * 2)

    def test_no_part_opens_on_the_loops_thread(self) -> None:
        # K7: a DASH pair's stream parts open off the loop, as every part does.
        self.routes.update({f"{CDN}/v.mp4": b"V" * 9, f"{CDN}/a.mp4": b"A" * 9})
        on_loop: list = []
        real = files.part

        @contextlib.contextmanager
        def part(dest, *args, **options):
            try:
                asyncio.get_running_loop()
                on_loop.append(Path(dest).name)
            except RuntimeError:
                pass
            with real(dest, *args, **options) as held:
                yield held

        def ffmpeg(argv):
            Path(argv[-1]).write_bytes(b"merged")
            return 0, "", ""

        with files.fake({"ffmpeg": ffmpeg}), mock.patch.object(files, "part", part):
            self.run_files(File(f"{CDN}/v.mp4", audio=f"{CDN}/a.mp4", name="v.mp4"))
        self.assertEqual(self.saved(), ["v.mp4"])
        self.assertEqual(on_loop, [])

    def test_two_targets_with_an_archive_each_both_save(self) -> None:
        # E13: each target's archive is unpacked, and each sends its `Saved`.
        for name in ("example_user", "example_creator"):
            body = io.BytesIO()
            with zipfile.ZipFile(body, "w") as archive:
                archive.writestr("inside.txt", name)
            self.routes[f"{CDN}/{name}.zip"] = body.getvalue()
        source = make_source("aaa", files=lambda site, context: [
            File(f"{CDN}/{site.target.value}.zip", name=f"{site.target.value}.zip")])
        self.report(source, targets=["aaa:example_user", "aaa:example_creator"])
        self.assertEqual(self.saved(), ["example_creator.zip", "example_user.zip"])

    def configure(self, text: str) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.toml").write_text(text, encoding="utf-8")

    def test_rate_slowest_and_free_space_from_the_config(self) -> None:
        body = b"\xff\xd8\xff" + bytes(999_997)
        self.routes[f"{CDN}/a.jpg"] = body
        self.configure('[defaults]\nrate = "100 KB/s"\n')
        self.run_files(File(f"{CDN}/a.jpg", name="a.jpg"))
        # A second's worth (100 KiB) goes at once; the rest waits about 8.8 s on the run's clock.
        self.assertGreaterEqual(self.now, 8.5)

        sent, requests = iter([body[:1000], body[1000:]]), []

        def slow(request):
            requests.append(request.headers.get("range"))
            if len(requests) == 1:
                def trickle():
                    yield next(sent)
                    self.fake.advance(31)
                    yield b"x"
                return net.reply(200, headers={"content-length": str(len(body))},
                                 chunks=trickle())
            return net.reply(206, content=body[1001:], headers={
                "content-range": f"bytes 1001-{len(body) - 1}/{len(body)}"})

        self.routes[f"{CDN}/b.jpg"] = slow
        self.configure('[defaults]\nslowest = "1 KB/s"\n')
        self.run_files(File(f"{CDN}/b.jpg", name="b.jpg"))
        # Under 1 KB/s over 30 s: cancelled, and tried again from where it was.
        self.assertEqual(requests, ["bytes=0-", "bytes=1001-"])

        self.configure("")
        with mock.patch.object(jobs.shutil, "disk_usage",
                               return_value=mock.Mock(free=10 << 20)), \
                self.assertRaisesRegex(DiskError, "not enough free space"):
            self.run_files(File(f"{CDN}/c.jpg", name="c.jpg", size=1 << 30))

    def test_exec_comes_from_the_user_file_alone(self) -> None:
        record = self.home.parent / "ran.txt"
        script = f"import sys; open({str(record)!r}, 'a').write(sys.argv[1] + '\\n')"
        user = self.home.parent / "user.toml"
        user.write_text(f'[defaults]\nexec = {json.dumps([sys.executable, "-c", script, "{path}"])}\n')
        self.routes.update({f"{CDN}/a.jpg": JPEG, f"{CDN}/b.jpg": JPEG + b"b"})
        # A home's own `exec` is ignored, with a warning: only the user's file runs programs.
        self.configure('[defaults]\nexec = ["false"]\n')
        with mock.patch("flnd.engine.config.user_file", return_value=user):
            self.run_files(File(f"{CDN}/a.jpg", name="a.jpg"))
        self.configure("")
        self.run_files(File(f"{CDN}/b.jpg", name="b.jpg"))
        folder = self.target_dir("aaa", "example_user")
        self.assertEqual(record.read_text().splitlines(), [str(folder / "a.jpg")])

    def test_four_files_are_hashed_at_once_on_four_threads(self) -> None:
        # K9: four new files meet four saved ones of their size; each thread's first hash waits
        # for three others, which one hasher thread for the whole process never gives.
        for n in range(8):
            self.routes[f"{CDN}/{n}.bin"] = bytes([n]) * 64
        self.run_files(*(File(f"{CDN}/{n}.bin", name=f"{n}.bin") for n in range(4)))
        threads: set[str] = set()
        met = threading.Barrier(4, timeout=5)
        real = hashlib.sha256

        def sha256(*args):
            made = real(*args)
            if args:
                return made

            class Waits:
                def update(self, data):
                    name = threading.current_thread().name
                    if name not in threads:
                        threads.add(name)
                        met.wait()
                    made.update(data)

                def digest(self):
                    return made.digest()

            return Waits()

        with mock.patch.object(files.hashlib, "sha256", sha256), \
                mock.patch.object(jobs.os, "cpu_count", return_value=8):
            self.run_files(*(File(f"{CDN}/{n}.bin", name=f"{n}.bin") for n in range(4, 8)))
        self.assertGreaterEqual(len(threads), 4, threads)

    def test_a_write_lock_held_elsewhere_never_stalls_the_loop(self) -> None:
        # E18: another process holds flnd.db's write lock for half a second while a post
        # commits; the loop's longest stall stays under 100 ms.
        self.run_files()  # makes flnd.db
        stalls = probe(self)
        locked, release = threading.Event(), threading.Event()

        def hold() -> None:
            other = sqlite3.connect(self.home / home.DB, isolation_level=None)
            other.execute("BEGIN IMMEDIATE")
            locked.set()
            release.wait(5)
            other.execute("ROLLBACK")
            other.close()

        holder = threading.Thread(target=hold)

        def answer(request):
            # The lock is taken as the file comes, so its post's commit meets it.
            holder.start()
            locked.wait(5)
            threading.Timer(0.5, release.set).start()
            return JPEG

        self.routes[f"{CDN}/a.jpg"] = answer
        try:
            self.run_files(File(f"{CDN}/a.jpg", name="a.jpg"))
        finally:
            release.set()
            holder.join()
        self.assertEqual(self.saved(), ["a.jpg"])
        self.assertTrue(stalls)
        self.assertLess(max(stalls), 0.1)


# Where a post's rows are written.
COMMIT = (Store, "commit")


def probe(case) -> list[float]:
    """The stalls of each run's loop, from a probe that wakes every 5 ms, in real time."""
    from flnd.engine import core

    stalls: list[float] = []
    real = core.shared_browser

    @contextlib.asynccontextmanager
    async def probed(settings):
        async def wake():
            while True:
                at = time.perf_counter()
                await asyncio.sleep(0.005)
                stalls.append(time.perf_counter() - at - 0.005)

        waking = asyncio.get_running_loop().create_task(wake())
        try:
            async with real(settings):
                yield
        finally:
            waking.cancel()

    case.enterContext(mock.patch.object(core, "shared_browser", probed))
    return stalls


class RealSchedulerTest(SaveCase):
    """The save on the scheduler's pools and retry rule: a busy part, a flaky join, a sidecar."""

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
