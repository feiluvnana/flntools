"""src/engine/save.py: a post's files through the save's nine stages.

Each post runs through a real `flnkit.net.Client` on `net.fake`, the run's scheduler
(`flnd.engine.jobs`), ffmpeg faked by `files.fake`, and `clock.Fake`, in a temporary home.
`EngineTest` runs it inside the engine.
"""

import asyncio
import builtins
import contextlib
import errno
import io
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from unittest import mock

from fakes import EngineCase, make_plugin

from flnd.engine import jobs, store
from flnd.engine.events import Failed, Saved, Skipped
from flnd.engine.jobs import Kind
from flnd.engine.save import Item, Saver
from flnd.errors import DiskError, NotFound
from flnd.plugin import File, Target
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
        # The run's commit connection, its flnd.db made now.
        self.store = store.open(self.tmp, loop=True)
        self.addCleanup(self.store.close)
        self.store.ready()
        self.sql = self.store._db.connection()
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

    def rows(self, columns="key, path, size, date") -> list[tuple]:
        return self.sql.execute(f"SELECT {columns} FROM files ORDER BY key").fetchall()

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

    def test_last_modified_dates_undated_and_estimated_dated_names(self) -> None:
        hdrs = {"Last-Modified": "Tue, 17 Mar 2026 08:00:00 GMT"}
        self.routes[f"{CDN}/u.jpg"] = net.reply(200, content=JPEG, headers=hdrs)
        self.routes[f"{CDN}/e.jpg"] = net.reply(200, content=JPEG, headers=hdrs)
        self.routes[f"{CDN}/s.jpg"] = net.reply(200, content=JPEG, headers=hdrs)
        self.routes[f"{CDN}/p.jpg"] = net.reply(200, content=JPEG, headers=hdrs)
        self.save(
            self.item("u.jpg", day=None, dated_name=True),
            self.item("2026-08-31_e.jpg", url=f"{CDN}/e.jpg", day=datetime(2026, 8, 31).date(),
                      estimated=True, dated_name=True),
            self.item("2026-03-17_s.jpg", url=f"{CDN}/s.jpg", day=datetime(2026, 3, 17).date(),
                      estimated=True, dated_name=True),
            self.item("p.jpg", day=None, dated_name=False),
        )
        names = sorted(p.name for p in (self.tmp / "media").iterdir())
        self.assertEqual(names, ["2026-03-17_e.jpg", "2026-03-17_s.jpg", "2026-03-17_u.jpg",
                                 "p.jpg"])
        rows = {k: (Path(p).name, d, e)
                for k, p, d, e in self.rows("key, path, date, estimated")}
        self.assertEqual(rows["ins:u.jpg"], ("2026-03-17_u.jpg", "2026-03-17", 0))
        self.assertEqual(rows["ins:2026-08-31_e.jpg"], ("2026-03-17_e.jpg", "2026-03-17", 0))
        self.assertEqual(rows["ins:2026-03-17_s.jpg"], ("2026-03-17_s.jpg", "2026-03-17", 0))
        self.assertEqual(rows["ins:p.jpg"], ("p.jpg", "2026-03-17", 0))

    def test_a_web_page_fails_the_file_and_keeps_no_part(self) -> None:
        self.routes[f"{CDN}/a.jpg"] = b"<!doctype html><title>log in</title>"
        self.save(self.item("a.jpg"))
        [failed] = self.events
        self.assertIsInstance(failed, Failed)
        self.assertIn("got a web page instead of a photo", str(failed.error))
        self.assertEqual(os.listdir(self.tmp / "media"), [])
        self.assertEqual(self.sql.execute("SELECT key, class FROM failures").fetchall(),
                         [("ins:a.jpg", "SiteError")])

    def test_the_mirrors_are_tried_in_order(self) -> None:
        self.routes.update({f"{CDN}/one.jpg": 500, f"{CDN}/two.jpg": JPEG})
        self.save(self.item("a.jpg", file=File(mirrors=[f"{CDN}/one.jpg", f"{CDN}/two.jpg"])))
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
        stop = threading.Event()

        async def fetch(part):
            await part.write(JPEG)
            # Ctrl-C between two writes: the second stops the run, and the first stays.
            stop.set()
            await part.write(b"more")

        self.save(self.item("a.jpg", file=File(fetch=fetch)))
        self.assertEqual((self.tmp / "media" / "a.jpg").read_bytes(), JPEG + b"more")
        stop.clear()
        with self.assertRaises(KeyboardInterrupt):
            self.save(self.item("b.jpg", file=File(fetch=fetch)), stop=stop)
        self.assertEqual((self.tmp / "media" / "b.jpg.part").read_bytes(), JPEG)

    def test_saver_releases_claims_on_commit(self) -> None:
        from flnd.engine.crawl import Claims
        claims = Claims()
        item = self.item("claimed.jpg")
        claims.claim(item.dest, item.key, "claimed.jpg")
        self.assertIn(item.dest, claims)
        self.routes[f"{CDN}/claimed.jpg"] = JPEG
        self.save(item, claims=claims)
        self.assertNotIn(item.dest, claims)
        self.assertNotIn(item.key, claims)

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
                 ("big.bin", 24 * mib, True, jobs.SEGMENTS, 2),
                 ("huge.bin", 96 * mib, True, jobs.SEGMENTS, jobs.SEGMENTS)]
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
        self.save(self.item("m.bin", file=File(mirrors=[f"{CDN}/one.bin", f"{CDN}/two.bin"]),
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


class CommitTest(SaveCase):
    def test_a_posts_rows_go_in_one_transaction(self) -> None:
        statements: list[str] = []
        self.sql.set_trace_callback(statements.append)
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            self.routes[f"{CDN}/{name}"] = JPEG
        self.save(self.item("a.jpg", count=3), self.item("b.jpg"), self.item("c.jpg"))
        self.assertEqual(statements.count("BEGIN IMMEDIATE"), 1)
        self.assertEqual(len(self.rows()), 3)
        self.assertEqual(self.sql.execute("PRAGMA busy_timeout").fetchone(), (0,))

    def test_a_commit_that_meets_a_lock_is_queued_again(self) -> None:
        other = sqlite3.connect(self.store.path, isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        tries, commit = [], self.store.commit

        def counted(*args, **options):
            tries.append(self.jobs.kinds[-1])
            if len(tries) == 3:
                other.execute("COMMIT")
            return commit(*args, **options)

        self.routes[f"{CDN}/a.jpg"] = JPEG
        with mock.patch.object(self.store, "commit", counted):
            self.save(self.item("a.jpg"))
        # The `WRITE` job, tried again at its priority, never a thread's wait.
        self.assertEqual(tries, [Kind.WRITE] * 3)
        self.assertEqual([row[0] for row in self.rows()], ["ins:a.jpg"])
        other.close()

    def test_the_log_is_checkpointed_every_so_many_rows_and_at_close(self) -> None:
        self.routes.update({f"{CDN}/a.jpg": JPEG, f"{CDN}/b.jpg": PNG})
        with mock.patch.object(store, "CHECKPOINT_ROWS", 2), \
                mock.patch.object(store.Store, "checkpoint", autospec=True) as checkpoint:
            self.save(self.item("a.jpg"), self.item("b.jpg"))
        self.assertEqual(checkpoint.call_count, 2)


class StopTest(SaveCase):
    def test_what_stops_the_run_and_what_fails_one_file(self) -> None:
        def full(self, *data, **options):
            raise OSError(errno.ENOSPC, "No space left on device")

        def refused(*args, **options):
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
        gone = self.sql.execute("SELECT state FROM files WHERE key = ?",
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

    def test_a_here_archive_is_unpacked_into_its_folder_and_deleted_once_extracted(self) -> None:
        body = io.BytesIO()
        with zipfile.ZipFile(body, "w") as archive:
            archive.writestr("top/inside.txt", "in")
        self.routes[f"{CDN}/a.zip"] = body.getvalue()
        self.routes[f"{CDN}/bad.zip"] = b"PK\x03\x04 not an archive"
        self.save(self.item("a.zip", kind=None, file=File(f"{CDN}/a.zip", unpack="here")),
                  self.item("bad.zip", kind=None, file=File(f"{CDN}/bad.zip", unpack="here")))
        media = self.tmp / "media"
        self.assertFalse((media / "a.zip").exists())
        self.assertEqual((media / "inside.txt").read_text(), "in")
        self.assertTrue((media / "bad.zip").is_file())
        rows = dict(self.rows("key, unpacked"))
        self.assertEqual(rows, {"ins:a.zip": "extracted", "ins:bad.zip": "failed"})
        [(made,)] = self.sql.execute(
            "SELECT extracted FROM files WHERE key = 'ins:a.zip'").fetchall()
        self.assertEqual(json.loads(made), [str(media / "inside.txt")])
        self.assertEqual(self.sql.execute(
            "SELECT extracted FROM files WHERE key = 'ins:bad.zip'").fetchone(), (None,))


class EngineTest(EngineCase):
    """The save inside a run of the engine: what stops the run, and what each file ends as."""

    def run_files(self, *found: File, targets=("aaa:example_user",)):
        return self.report(make_plugin("aaa", files=list(found)), targets=list(targets))

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
        async def fetch(part):
            await part.write(JPEG)

        caps: list = []
        real = jobs.Scheduler.run

        async def run(scheduler, job):
            if job.kind is Kind.FILE:
                caps.append((job.host, scheduler.limits.cap(job.host).most))
            return await real(scheduler, job)

        source = make_plugin("aaa", files=[File(fetch=fetch, name="a.jpg"),
                                                File(fetch=fetch, name="b.jpg")],
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
        source = make_plugin("aaa", files=lambda ctx, context: [
            File(f"{CDN}/{ctx.target.value}.zip", name=f"{ctx.target.value}.zip")])
        self.report(source, targets=["aaa:example_user", "aaa:example_creator"])
        self.assertEqual(self.saved(), ["example_creator.zip", "example_user.zip"])

    def configure(self, text: str) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.toml").write_text(text, encoding="utf-8")

    def test_slowest_and_free_space_from_the_config(self) -> None:
        body = b"\xff\xd8\xff" + bytes(999_997)
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

    def test_a_write_lock_held_elsewhere_never_stalls_the_loop(self) -> None:
        # E18: another process holds flnd.db's write lock for half a second while a post
        # commits; the loop's longest stall stays under 100 ms.
        self.run_files()  # makes flnd.db
        stalls = probe(self)
        locked, release = threading.Event(), threading.Event()

        def hold() -> None:
            other = sqlite3.connect(self.home / store.DB, isolation_level=None)
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
COMMIT = (store.Store, "commit")


def probe(case) -> list[float]:
    """The stalls of each run's loop, from a probe that wakes every 5 ms, in real time."""
    from flnd.engine import _engine as core

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
    """The save on the scheduler's pools and retry rule: a busy part and a flaky join."""

    def test_a_busy_part_and_a_flaky_join(self) -> None:
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
            self.save(self.item("a.jpg"), self.item("b.jpg"),
                      self.item("v.mp4", file=dash, kind=None))
        self.assertEqual(sorted(type(event).__name__ for event in self.events),
                         ["Saved", "Saved", "Skipped"])
        self.assertIsNone(self.jobs._stop)
        self.assertEqual(len(ran), 2)

    def test_a_split_segment_retry_never_deadlocks_when_host_cap_drops_to_one(self) -> None:
        # Regression: a segmented download whose host cap halves to 1 after a segment error
        # must not deadlock waiting for the segment while holding the host's only cap place.
        body = os.urandom(24 << 20)
        failed = False

        def answer(request):
            nonlocal failed
            span = request.headers.get("range", "bytes=0-")
            first, _, last = span.removeprefix("bytes=").partition("-")
            start, end = int(first), int(last) + 1 if last else len(body)
            piece = body[start:end]

            def chunks():
                nonlocal failed
                for at in range(0, len(piece), 256 << 10):
                    if start > 0 and not failed and at >= (512 << 10):
                        failed = True
                        # Simulate an overloaded host halving the cap down to 1 before retry.
                        cap = self.jobs.limits.cap("cdn.example.com")
                        cap.value = 1
                        raise ConnectionResetError("reset")
                    self.clock.advance(0.25)
                    yield piece[at:at + (256 << 10)]

            headers = {"etag": '"v1"', "accept-ranges": "bytes"}
            if start or end < len(body):
                headers["content-range"] = f"bytes {start}-{end - 1}/{len(body)}"
                return net.reply(206, headers=headers, chunks=chunks())
            return net.reply(200, headers={**headers, "content-length": str(len(body))},
                             chunks=chunks())

        self.routes[f"{CDN}/big.mp4"] = answer
        with mock.patch.object(jobs, "NETWORK_WAITS", (0.01,)):
            self.save(self.item("big.mp4", kind=None), timeout=1.5)
        self.assertTrue(failed, "segment 1 never triggered the reset")
        self.assertEqual([type(event) for event in self.events], [Saved])
        self.assertEqual((self.tmp / "media" / "big.mp4").read_bytes(), body)

    def test_resuming_segmented_part_never_deadlocks_at_cap_one(self) -> None:
        body = os.urandom(24 << 20)
        dest = self.tmp / "media" / "resumed.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(body[:12 << 20] + b"\x00" * (12 << 20))
        side = dest.with_name(dest.name + ".part.json")
        side.write_text(json.dumps({
            "etag": '"v1"', "last_modified": "", "size": len(body),
            "segments": [[0, 12 << 20, 12 << 20], [12 << 20, len(body), 0]],
        }))

        def answer(request):
            span = request.headers.get("range", "bytes=0-")
            first, _, last = span.removeprefix("bytes=").partition("-")
            start, end = int(first), int(last) + 1 if last else len(body)
            return net.reply(206, content=body[start:end], headers={
                "etag": '"v1"', "accept-ranges": "bytes",
                "content-range": f"bytes {start}-{end - 1}/{len(body)}",
            })

        self.routes[f"{CDN}/resumed.mp4"] = answer
        self.jobs.limits.cap("cdn.example.com").value = 1
        self.save(self.item("resumed.mp4", kind=None), timeout=1.5)
        self.assertEqual([type(event) for event in self.events], [Saved])
        self.assertEqual(dest.read_bytes(), body)


if __name__ == "__main__":
    unittest.main()
