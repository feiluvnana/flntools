"""Live stats: `flnd.Stats` events and `run.stats`, from a faked run and from a meter alone.

`MeterTest` drives `flnd._stats.Meter` with small stand-ins for what a run tracks (limits, a
browser, a board, a chain, transfers, the home) on a fake clock, so each field of a sample is
checked against numbers the test chose. `RunStatsTest` reads a whole run over the fake network
and checks the same fields as the engine fills them: the listing's pages and posts, the CDN
host's cap, the source's pace, the totals, and that `wait()` samples nothing.
"""

import datetime
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import flnd
from flnd import _loop, _net, _stats
from flnd import _time
from flnd._events import Listing, encode
from flnd.source import File, Post, Source, Target, target
from test_library import JPEG, LibraryCase
from test_engine import CDN

TARGET = Target("exs:example_user", kind="user")


class _Bar:
    """A transfer's progress bar, as `flnd._ui.Row` holds it: a count and a total."""

    def __init__(self, count, total):
        self.count, self.total = count, total


class _Transfer:
    """What `flnd._transfer` tracks for a file in flight."""

    def __init__(self, label, host, done, total, resumed=0):
        self.label, self.host, self.resumed = label, host, resumed
        self.bar = _Bar(done, total)


class _Limits:
    """`flnd._net.Limits`'s two snapshots, recording the thread that reads them."""

    def __init__(self, load, pauses):
        self._load, self._pauses, self.threads = load, pauses, []

    @property
    def load(self):
        self.threads.append(threading.current_thread())
        return self._load

    @property
    def pauses(self):
        return self._pauses


def _source(code, limits):
    """A `flnd.site.SourceRun` as the meter reads it: its code and its files' limits."""
    return SimpleNamespace(source=SimpleNamespace(code=code), files=SimpleNamespace(limits=limits))


class _Board:
    """A `flnd.job.Board`'s two snapshots."""

    def __init__(self, rows, queued):
        self.rows, self.waiting = rows, queued

    def listing(self):
        return list(self.rows)

    def queued(self):
        return self.waiting


class MeterTest(unittest.TestCase):
    def setUp(self):
        self.fake = self.enterContext(_time.use(_time.Fake()))
        self.meter = _stats.Meter()
        self.meter.start()
        self.home = Path(self.enterContext(TemporaryDirectory()))

    def poll(self, **counts):
        values = {"saved": 0, "skipped": 0, "failed": 0, "bytes": 0, **counts}
        return self.meter.poll(**values)

    def test_a_sample_comes_once_a_second(self):
        self.assertIsNone(self.poll())
        self.fake.advance(0.5)
        self.assertIsNone(self.poll())
        self.fake.advance(0.5)
        first = self.poll(saved=3, skipped=2, failed=1, bytes=4096, stopping="soon",
                          suspended=True)
        self.assertEqual((first.saved, first.skipped, first.failed, first.bytes),
                         (3, 2, 1, 4096))
        self.assertEqual((first.elapsed, first.stopping, first.suspended), (1.0, "soon", True))
        self.assertIsNone(self.poll())
        self.fake.advance(1.0)
        self.assertEqual(self.poll().elapsed, 2.0)

    def test_hosts_and_pauses_come_from_each_sources_limits(self):
        limits = _Limits({"cdn.example.com": _net.Load(8, 8, 3, 32),
                          "media.example.com": _net.Load(4, 1, 0, 16)},
                         (_net.Pause("www.example.com", 2.5, _net.PACE),))
        self.meter.add(_stats.SOURCE, _source("exs", limits))
        self.meter.add(_stats.WAIT, ("exs", "exs:example_user", 100.0, "the site's quiet hours"))
        self.fake.advance(1.0)
        stats = self.poll()
        hosts = stats.transfers.hosts
        self.assertEqual([(h.host, h.cap, h.busy, h.waiting, h.most) for h in hosts],
                         [("cdn.example.com", 8, 8, 3, 32), ("media.example.com", 4, 1, 0, 16)])
        self.assertEqual([(p.source, p.host, p.seconds, p.reason) for p in stats.pauses],
                         [("exs", "exs:example_user", 99.0, "the site's quiet hours"),
                          ("exs", "www.example.com", 2.5, "pace")])

    def test_the_loops_counters_are_read_on_the_loop(self):
        limits = _Limits({}, ())
        with _loop.Loop() as loop:
            loop.run(_nothing())
            self.meter.add(_stats.LOOP, loop)
            self.meter.add(_stats.SOURCE, _source("exs", limits))
            self.fake.advance(1.0)
            self.assertIsNotNone(self.poll())
        self.assertEqual([thread.name for thread in limits.threads], ["flnd-loop"])

    def test_a_loop_not_started_is_never_started_by_a_sample(self):
        loop = _loop.Loop()
        self.meter.add(_stats.LOOP, loop)
        self.fake.advance(1.0)
        self.assertIsNotNone(self.poll())
        self.assertFalse(loop.running)

    def test_transfers_their_speed_and_resumes(self):
        moving = _Transfer("example_user/a.mp4", "cdn.example.com", 0, 4000)
        resumed = _Transfer("example_user/b.mp4", "cdn.example.com", 6000, 10000, resumed=6000)
        small = _Transfer("example_user/c.jpg", "cdn.example.com", 0, None)
        for item in (moving, resumed, small):
            self.meter.add(_stats.TRANSFER, item)
        self.fake.advance(1.0)
        first = self.poll()
        self.assertEqual([(f.name, f.done, f.total, f.resumed) for f in first.transfers.files],
                         [("example_user/a.mp4", 0, 4000, False),
                          ("example_user/b.mp4", 6000, 10000, True),
                          ("example_user/c.jpg", 0, None, False)])
        # What a resumed part held isn't speed.
        self.assertEqual((first.speed, first.transfers.running, first.transfers.resuming),
                         (0.0, 3, 1))
        moving.bar.count, resumed.bar.count = 2000, 7000
        # A file that starts and ends between two samples still counts its bytes.
        small.bar.count = 500
        self.meter.remove(_stats.TRANSFER, small)
        self.fake.advance(2.0)
        second = self.poll()
        self.assertEqual([f.speed for f in second.transfers.files], [1000.0, 500.0])
        self.assertEqual(second.speed, (2000 + 1000 + 500) / 2.0)

    def test_listing_queue_steps_tabs_and_disk(self):
        rows = [Listing(TARGET, "posts", pages=3, posts=40, found=52, saved=10, state="listing")]
        self.meter.add(_stats.BOARD, _Board(rows, queued=7))
        self.meter.add(_stats.CHAIN, SimpleNamespace(jobs={"photo": (2, 5), "archive": (1, 0),
                                                          "video": (1, 3)}))
        self.meter.add(_stats.BROWSER, SimpleNamespace(tabs=3))
        self.meter.add(_stats.HOME, self.home / "not-made-yet")
        self.fake.advance(1.0)
        stats = self.poll()
        self.assertEqual(stats.listing, tuple(rows))
        self.assertEqual(stats.transfers.queued, 7)
        system = stats.system
        self.assertEqual((system.tabs, system.cap, system.pillow, system.unpack, system.steps),
                         (3, _stats.TABS, 2, 1, 8))
        self.assertGreater(system.disk, 0)

    def test_the_process_numbers_come_from_psutil(self):
        self.fake.advance(1.0)
        first = self.poll()
        self.assertGreater(first.system.ram, 1 << 20)
        self.assertEqual((first.system.cpu, first.system.ffmpeg, first.system.browser),
                         (0.0, 0, 0))
        sum(range(200_000))
        self.fake.advance(1.0)
        self.assertGreaterEqual(self.poll().system.cpu, 0.0)

    def test_the_eta_is_known_once_every_listing_is_done(self):
        listing = Listing(TARGET, "posts", found=10, saved=4, state="listing")
        board = _Board([listing], queued=0)
        self.meter.add(_stats.BOARD, board)
        self.fake.advance(2.0)
        self.assertIsNone(self.poll(saved=4).eta)
        board.rows = [Listing(TARGET, "posts", found=10, saved=4, skipped=2, state="finishing")]
        self.fake.advance(2.0)
        # 4 files ended in 4 seconds, and 4 are left.
        self.assertEqual(self.poll(saved=4).eta, 4.0)
        board.rows = [Listing(TARGET, "posts", found=10, saved=8, skipped=2, state="done")]
        self.fake.advance(1.0)
        self.assertIsNone(self.poll(saved=8).eta)

    def test_tracking_outside_a_run_does_nothing(self):
        self.assertIsNone(_stats.METER.get())
        _stats.track(_stats.BOARD, object())
        with _stats.tracked(_stats.SOURCE, object()):
            pass

    def test_tracked_items_leave_with_their_block(self):
        token = _stats.METER.set(self.meter)
        self.addCleanup(_stats.METER.reset, token)
        limits = _Limits({"cdn.example.com": _net.Load(8, 0, 0, 32)}, ())
        with _stats.tracked(_stats.SOURCE, _source("exs", limits)):
            self.fake.advance(1.0)
            self.assertEqual(len(self.poll().transfers.hosts), 1)
        self.fake.advance(1.0)
        self.assertEqual(self.poll().transfers.hosts, ())


async def _nothing():
    return None


def gated_source(gate, *, pace=30.0):
    """A source whose `posts` reads one page, yields two posts, then waits for `gate`."""

    class Gated(Source):
        code, name, domains, folder, key = "sts", "Gated", ("sts.example",), "{category}", "{post}"
        api = 1

        @target("sts:{user}")
        def user(self, site, user):
            return user

        @user.category
        def posts(self, site, user):
            # One page of a paged listing: its cursor ends it.
            for _ in site.pages("https://sts.example/api/feed", cursor="next"):
                pass
            for post_id in ("p1", "p2"):
                yield Post(post_id, [File(f"{CDN}/{post_id}.jpg")], date=datetime.date(2026, 3, 1))
            gate.wait(10)
            yield Post("p3", [File(f"{CDN}/p3.jpg")], date=datetime.date(2026, 3, 1))

    Gated.pace = pace
    return Gated


class RunStatsTest(LibraryCase):
    def setUp(self):
        super().setUp()
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)
        self.fl.add(gated_source(self.gate))
        self.routes["https://sts.example/api/feed"] = _net.reply(200, json={"next": None})

    def read(self, run, *, at_two_saved, at_stats=lambda: None):
        """Iterate `run`; once two files are saved or skipped, call `at_two_saved()`, then move
        the clock on a second and a half; at the first `Stats`, call `at_stats()` and open the
        gate.

        Returns:
            Every event, and the first `Stats`.
        """
        events, stats, called = [], None, False
        for event in run:
            events.append(event)
            # Saved, or already saved by an earlier run of the test.
            ended = sum(isinstance(seen, (flnd.Saved, flnd.Skipped)) for seen in events)
            if ended == 2 and not called:
                called = True
                at_two_saved()
                self.fake.advance(1.5)
            if isinstance(event, flnd.Stats) and stats is None:
                stats = event
                at_stats()
                self.gate.set()
        return events, stats

    def test_a_read_run_yields_stats_and_keeps_the_latest(self):
        run = self.fl.download("sts:example_user")
        self.assertIsNone(run.stats)
        events, stats = self.read(run, at_two_saved=lambda: None)
        self.assertEqual(sum(isinstance(event, flnd.Stats) for event in events), 1)
        self.assertIs(run.stats, stats)
        # The run: the two files saved before the sample, their bytes and their speed.
        self.assertEqual((stats.saved, stats.skipped, stats.failed, stats.bytes),
                         (2, 0, 0, 2 * len(JPEG)))
        self.assertEqual(stats.elapsed, 1.5)
        self.assertAlmostEqual(stats.speed, 2 * len(JPEG) / 1.5)
        self.assertIsNone(stats.eta)
        self.assertEqual((stats.stopping, stats.suspended), ("", False))
        # The listing: one page read, two posts, two files found and saved, still going.
        [row] = stats.listing
        self.assertEqual((str(row.target), row.category, row.pages, row.posts, row.found,
                          row.saved, row.streak, row.state),
                         ("sts:example_user", "posts", 1, 2, 2, 2, 0, "listing"))
        # The downloads: nothing in flight, the CDN host at its starting cap.
        transfers = stats.transfers
        self.assertEqual((transfers.running, transfers.queued, transfers.resuming, transfers.files),
                         (0, 0, 0, ()))
        self.assertEqual([(h.host, h.cap, h.busy, h.waiting, h.most) for h in transfers.hosts],
                         [("cdn.example", 8, 0, 0, 32)])
        # The pacing: the source's own host waits out its pace of one page every 30 s.
        [pause] = stats.pauses
        self.assertEqual((pause.source, pause.host, pause.reason), ("sts", "sts.example", "pace"))
        self.assertAlmostEqual(pause.seconds, 30.0 - 1.5)
        # The machine: no browser opened, flnd's own RAM, free disk on the home's volume.
        system = stats.system
        self.assertEqual((system.tabs, system.cap, system.browser, system.ffmpeg),
                         (0, 6, 0, 0))
        self.assertGreater(system.ram, 0)
        self.assertGreater(system.disk, 0)
        self.assertTrue(encode(stats).startswith(b'{"type":"Stats"'))
        self.assertEqual(run.report.saved, 3)

    def test_suspend_and_stop_show_in_the_next_sample(self):
        run = self.fl.download("sts:example_user")
        _, stats = self.read(run, at_two_saved=run.suspend, at_stats=run.resume)
        self.assertTrue(stats.suspended)
        # Resumed, the third file went on.
        self.assertEqual(run.report.saved, 3)
        self.assertEqual(stats.stopping, "")

        self.gate.clear()
        run = self.fl.download("sts:example_user")

        def stop():
            run.suspend()
            run.stop()

        _, stats = self.read(run, at_two_saved=stop)
        # A stop ends a suspension, so the run can get to its stop.
        self.assertEqual((stats.stopping, stats.suspended), ("soon", False))
        self.assertTrue(run.report.stopped)

    def test_wait_with_nobody_reading_samples_nothing(self):
        self.gate.set()
        fake = self.fake

        class Slow(Source):
            code, name, domains, folder, key = "slw", "Slow", ("slw.example",), "{category}", "{post}"
            api = 1

            @target("slw:{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                fake.advance(5.0)
                yield Post("p1", [File(f"{CDN}/p1.jpg")], date=datetime.date(2026, 3, 1))

        self.fl.add(Slow)
        run = self.fl.download("slw:example_user")
        self.assertEqual(run.wait().saved, 1)
        self.assertIsNone(run.stats)


class SuspendTest(unittest.TestCase):
    def test_a_suspended_limit_holds_new_requests_until_resumed(self):
        import asyncio

        limits = _net.Limits()
        limits.suspended = threading.Event()
        limits.suspended.set()

        async def one():
            async with limits.slot("cdn.example.com"):
                return True

        async def check():
            task = asyncio.ensure_future(one())
            await asyncio.sleep(0.25)
            self.assertFalse(task.done())
            limits.suspended.clear()
            return await asyncio.wait_for(task, 1.0)

        self.assertTrue(asyncio.run(check()))

    def test_a_stop_ends_the_wait_of_a_suspended_request(self):
        import asyncio

        limits = _net.Limits()
        limits.suspended, stop = threading.Event(), threading.Event()
        limits.suspended.set()
        threading.Timer(0.15, stop.set).start()

        async def one():
            async with limits.slot("cdn.example.com", stop):
                return True

        # `Stopped` is a KeyboardInterrupt, which asyncio lets out of the loop as it is.
        with self.assertRaises(flnd.Stopped):
            asyncio.run(one())


class PausesTest(unittest.TestCase):
    """`Limits.pauses`: a paced host's next token, a site's hold, and a client's back-off."""

    def setUp(self):
        self.fake = self.enterContext(_time.use(_time.Fake()))

    def test_pace_hold_and_back_off(self):
        import asyncio

        limits = _net.Limits(paced=lambda host: host == "www.example.com", rate=0.5)
        asyncio.run(_take(limits))
        [pause] = limits.pauses
        self.assertEqual((pause.host, pause.seconds, pause.reason),
                         ("www.example.com", 2.0, "pace"))
        limits.blocked("www.example.com", 60.0)
        [pause] = limits.pauses
        self.assertEqual((pause.seconds, pause.reason), (60.0, "Retry-After"))
        self.fake.advance(61.0)
        # The hold is over, but the rate stays halved for a while: a back-off.
        self.assertEqual(limits.pauses, ())
        asyncio.run(_take(limits))
        [pause] = limits.pauses
        self.assertEqual((pause.seconds, pause.reason), (4.0, "back-off"))
        with limits.holding("cdn.example.com", 8.0, _net.BACK_OFF):
            self.assertEqual(limits.pauses[0], _net.Pause("cdn.example.com", 8.0, "back-off"))
        self.assertEqual(len(limits.pauses), 1)


async def _take(limits):
    async with limits.slot("www.example.com"):
        pass


if __name__ == "__main__":
    unittest.main()
