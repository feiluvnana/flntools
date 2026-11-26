"""The scheduler (`flnd.engine.jobs`): each pool's numbers, priorities, the retry policy, holds,
segments, `rate`, `slowest`, the free-space stop and the hasher pool, on the fake clock."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from plugin_kit import fake_clock

from flnd import errors
from flnd.engine import jobs
from flnd.engine.jobs import (
    Cap,
    Job,
    Kind,
    Pool,
    Scheduler,
    Segment,
    Segments,
    Speed,
)
from flnd.engine.run import Tally
from flnkit import clock, files

MIB = 1 << 20


def run(main, *, cpus: int = 8, **options):
    """`main(scheduler, fake)` on a new loop under the fake clock, with `cpus` cores."""
    async def body():
        with Scheduler(**options) as scheduler:
            return await main(scheduler, fake)

    with mock.patch.object(jobs.os, "cpu_count", return_value=cpus), fake_clock() as fake:
        return asyncio.run(body())


async def peak(scheduler: Scheduler, made: list[Job]) -> int:
    """The most of `made` running at once, once every one that can start has started."""
    running = top = 0
    gate = asyncio.Event()

    async def work():
        nonlocal running, top
        running += 1
        top = max(top, running)
        await gate.wait()
        running -= 1

    for job in made:
        job.work = work
    tasks = [asyncio.ensure_future(scheduler.run(job)) for job in made]
    for _ in range(5):
        await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(*tasks)
    return top


def job(kind: Kind = Kind.PAGE, **fields) -> Job:
    fields.setdefault("plugin", "ex")
    fields.setdefault("host", "example.com")
    return Job(kind=kind, work=fields.pop("work", None), **fields)


class PoolTest(unittest.TestCase):
    def test_each_pools_numbers(self):
        rows = [  # (cores, kind, places)
            (8, Kind.CALL, 8), (64, Kind.CALL, 8), (8, Kind.UNPACK, 1),
            (8, Kind.MERGE, 4), (1, Kind.MERGE, 1),
        ]
        for cpus, kind, size in rows:
            with self.subTest(cpus=cpus, kind=kind):
                self.assertEqual(run(lambda s, f, k=kind: _size(s, k), cpus=cpus), size)

    def test_how_many_run_at_once(self):
        def tabs(n, plugins=1, scroll=False):
            return [job(Kind.TAB, plugin=f"p{i % plugins}", scroll=scroll) for i in range(n)]

        rows = {  # name: (jobs, options, most at once)
            "merge lane": ([job(Kind.MERGE) for _ in range(9)], {}, 4),
            "tab budget": (tabs(9), {}, jobs.TABS),
            "scrolling tabs of one plugin": (tabs(5, scroll=True), {}, 2),
            "scrolling tabs of two plugins": (
                tabs(6, 2, scroll=True), {}, min(jobs.TABS, 2 * jobs.SCROLLING)),
            "a host's cap starts at 8": ([job(Kind.FILE) for _ in range(20)], {}, 8),
            "a host's own limit": (
                [job(Kind.FILE, host="cdn.example.com") for _ in range(9)],
                {"limits": {"example.com": 3}}, 3),
            "places per target": (
                [job(Kind.FILE, host=f"h{i}.example.com", target="t") for i in range(40)],
                {}, 12),
            "places follow the largest limit": (
                [job(Kind.FILE, host=f"h{i}.example.com", target="t") for i in range(50)],
                {"limits": {"other.example": 40}}, 40),
        }
        for name, (made, options, most) in rows.items():
            with self.subTest(name):
                self.assertEqual(run(lambda s, f, m=made: peak(s, m), **options), most)

    def test_a_targets_queue_holds_two_jobs_a_place(self):
        async def main(scheduler, fake):
            queue, places = scheduler._target("t")
            return queue.size, places.size, scheduler.limits.cap("x.example.com").most

        self.assertEqual(run(main), (24, 12, 12))

    def test_a_warm_tab_opens_only_with_one_tab_free(self):
        async def main(scheduler, fake):
            gate = asyncio.Event()
            seen = []

            async def work():
                seen.append(scheduler.warm)
                await gate.wait()

            tasks = [asyncio.ensure_future(scheduler.run(job(Kind.TAB, work=work)))
                     for _ in range(jobs.TABS - jobs.WARM_FREE + 1)]
            for _ in range(2):
                await asyncio.sleep(0)
            seen.append(scheduler.warm)
            gate.set()
            await asyncio.gather(*tasks)
            return seen

        free = jobs.TABS - jobs.WARM_FREE
        self.assertEqual(run(main), [True] * free + [False, False])

    def test_a_tab_waiting_to_scroll_holds_no_tab(self):
        async def main(scheduler, fake):
            scheduler.tabs = Pool(6)
            gate, started = asyncio.Event(), []

            async def scroll():
                await gate.wait()

            async def other():
                started.append(scheduler.tabs.busy)

            tasks = [asyncio.ensure_future(scheduler.run(job(Kind.TAB, scroll=True, work=scroll)))
                     for _ in range(6)]
            tasks.append(asyncio.ensure_future(scheduler.run(
                job(Kind.TAB, plugin="b", work=other))))
            for _ in range(5):
                await asyncio.sleep(0)
            gate.set()
            await asyncio.gather(*tasks)
            return started

        self.assertEqual(run(main), [3])

    def test_closing_waits_for_the_threads_off_the_loop(self):
        async def main():
            scheduler = Scheduler()
            real = jobs.ThreadPoolExecutor.shutdown
            with mock.patch.object(jobs.ThreadPoolExecutor, "shutdown", autospec=True,
                                   side_effect=real) as shut:
                with scheduler:
                    async def work():
                        return await scheduler.on(Kind.CALL, threading.get_ident)

                    await scheduler.run(job(Kind.CALL, work=work))
                exited = shut.call_args.kwargs
                await scheduler.close()
                return exited, shut.call_args.kwargs

        with fake_clock():
            exited, closed = asyncio.run(main())
        self.assertEqual(exited, {"wait": False})
        self.assertEqual(closed, {})

    def test_a_waiter_cancelled_as_its_place_is_given_back_stays_cancelled(self):
        async def check(holder, take) -> None:
            await take()
            waiter = asyncio.ensure_future(take())
            await asyncio.sleep(0)
            waiter.cancel()
            holder.give()  # before the waiter's own step runs
            with self.assertRaises(asyncio.CancelledError):
                await waiter

        pool, cap = Pool(1), Cap(1, most=1)
        asyncio.run(check(pool, lambda: pool.take(0)))
        asyncio.run(check(cap, cap.take))

    def test_kinds_take_their_priorities(self):
        expected = {Kind.PAGE: 1, Kind.TAB: 1, Kind.FILE: 2, Kind.UNPACK: 3,
                    Kind.MERGE: 3}
        for kind, priority in expected.items():
            with self.subTest(kind):
                self.assertEqual(job(kind).priority, priority)

    def test_lower_priority_runs_first_and_equal_ones_in_order(self):
        async def main():
            pool, order = Pool(1), []
            await pool.take(0)

            async def wait(name, priority):
                async with pool.place(priority):
                    order.append(name)

            tasks = [asyncio.ensure_future(wait(name, priority)) for name, priority in
                     [("file1", 2), ("page1", 1), ("step", 3), ("retry", 0), ("page2", 1),
                      ("file2", 2)]]
            await asyncio.sleep(0)
            pool.give()
            await asyncio.gather(*tasks)
            return order

        self.assertEqual(asyncio.run(main()),
                         ["retry", "page1", "page2", "file1", "file2", "step"])

    def test_each_device_writes_on_one_thread_of_its_own(self):
        devices = {"a": 1, "b": 1, "c": 2}

        def stat(folder):
            return SimpleNamespace(st_dev=devices[Path(folder).name])

        def write():
            return threading.current_thread().name

        async def main(scheduler, fake):
            names = []
            for d in "abca":
                path = Path(f"/{d}/x.part")

                async def work(path=path):
                    return await scheduler.on(Kind.WRITE, write, path=path)

                names.append(await scheduler.run(job(Kind.WRITE, path=path, work=work)))
            return names

        with mock.patch.object(jobs.os, "stat", side_effect=stat) as stat_calls:
            names = run(main)
        self.assertEqual(names[0], names[1])
        self.assertEqual(names[0], names[3])
        self.assertNotEqual(names[0], names[2])
        self.assertEqual(stat_calls.call_count, 3)

    def test_a_page_waits_for_its_buckets_pace(self):
        async def main(scheduler, fake):
            starts = []

            async def work():
                starts.append(clock.now())

            for bucket in ["a", "a", "a", "b"]:
                await scheduler.run(job(bucket=bucket, work=work))
            return starts

        self.assertEqual(run(main), [0.0, 0.5, 1.0, 1.0])
        self.assertEqual(run(main, pace={"ex": 2.0}), [0.0, 2.0, 4.0, 4.0])


async def _size(scheduler: Scheduler, kind: Kind) -> int:
    return scheduler.pools[kind].size


# ---------------------------------------------------------------------------------------------
# The retry policy
# ---------------------------------------------------------------------------------------------


def raising(*outcomes):
    """Work that raises each outcome in turn that is an exception, and returns the first other."""
    left = list(outcomes)
    calls = []

    async def work():
        calls.append(clock.now())
        outcome = left.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    work.calls = calls
    return work


def blocked(wait=None, status=None):
    return errors.Blocked("blocked", wait=wait, status=status)


def wall():
    return errors.LoginError("landed on a wall", retryable=True)


def retryable(error):
    error.retryable = True
    return error


def broken():
    """What the loader raises for a plugin class that is broken."""
    error = errors.PluginError("the class declares no domains")
    error.broken = True
    return error


def network():
    return errors.NetworkError("reset", retryable=True)


class RetryTest(unittest.TestCase):
    def outcome(self, *outcomes, kind=Kind.PAGE, **options):
        """What a job with these outcomes ends with, the waits it slept, and the scheduler."""
        async def main(scheduler, fake):
            try:
                result = await scheduler.run(job(kind, work=raising(*outcomes)))
            except errors.Error as error:
                result = type(error)
            return result, list(fake.slept), scheduler

        return run(main, **options)

    def test_each_row(self):
        rows = {  # name: (outcomes, options, result, waits)
            "a 429 holds its wait": ((blocked(12, 429), "ok"), {}, "ok", [12]),
            "a 429 with no wait holds 30 s": ((blocked(None, 429), "ok"), {}, "ok", [30]),
            "a 429 holds at most an hour": ((blocked(7200, 429), "ok"), {}, "ok", [3600]),
            "a Retry-After of 0 holds nothing but the halved pace": (
                (blocked(0.0, 429), "ok"), {}, "ok", [1.0]),
            "a third 429 in a row is a block round": (
                (blocked(5, 429), blocked(5, 429), blocked(5, 429), "ok"), {}, "ok",
                [5, 5, 30]),
            "a block holds 30, 120, 480 s, then fails": (
                (blocked(), blocked(), blocked(), blocked()), {}, errors.Blocked,
                [30, 120, 480]),
            "a block holds the larger wait": ((blocked(200), "ok"), {}, "ok", [200]),
            "a wall holds 30 s then 120 s": ((wall(), wall(), "ok"), {}, "ok", [30, 120]),
            "a third wall fails": ((wall(), wall(), wall()), {}, errors.LoginError, [30, 120]),
            "a 401 fails at once": ((errors.LoginError("401"),), {}, errors.LoginError, []),
            "hidden fails": ((errors.Hidden("hidden"),), {}, errors.Hidden, []),
            "not found fails": ((errors.NotFound("gone"),), {}, errors.NotFound, []),
            "changed is never retried": (
                (errors.Changed("new shape", retryable=True), "ok"), {}, errors.Changed, []),
            "a final network error fails at once": (
                (errors.NetworkError("bad url"), "ok"), {}, errors.NetworkError, []),
            "a 4xx fails at once": (
                (errors.SiteError("400", status=400), "ok"), {}, errors.SiteError, []),
            "a failing tool fails its job": (
                (files.RunError("ffmpeg failed", code=1),), {}, files.RunError, []),
            "a tool failing retryably backs off, merges too": (
                (retryable(files.RunError("ffmpeg failed")), "ok"), {"kind": Kind.MERGE}, "ok",
                [1.0]),
            "any retryable error backs off, in any kind": (
                (errors.Error("busy", retryable=True), "ok"), {"kind": Kind.UNPACK}, "ok", [1.0]),
            "a missing tool is never retried": (
                (retryable(files.MissingError("ffmpeg isn't installed")), "ok"), {},
                files.MissingError, []),
            "a plugin's bug fails its job": (
                (errors.PluginError("bug"), "ok"), {}, errors.PluginError, []),
            "a broken plugin class fails": ((broken(),), {}, errors.PluginError, []),
        }
        with mock.patch.object(jobs.random, "uniform", return_value=1.0):
            for name, (outcomes, options, result, waits) in rows.items():
                with self.subTest(name):
                    got, slept, _ = self.outcome(*outcomes, **options)
                    self.assertEqual(got, result)
                    self.assertEqual(slept, waits)

    def test_network_failures_and_5xx_wait_1_2_4_8_s_with_jitter(self):
        for error in (network(), errors.SiteError("502", status=502, retryable=True)):
            for tries, result in ((4, "ok"), (5, type(error))):
                with self.subTest(error=str(error), tries=tries):
                    got, slept, _ = self.outcome(*[error] * tries, "ok")
                    self.assertEqual(got, result)
                    self.assertEqual(len(slept), 4)
                    for wait, base in zip(slept, (1, 2, 4, 8), strict=True):
                        self.assertTrue(base * 0.5 <= wait <= base * 1.5, (wait, base))

    def test_a_full_disk_a_missing_tool_or_a_broken_plugin_stops_the_run(self):
        for error in (errors.DiskError("full"), files.MissingError("ffmpeg isn't installed"),
                      retryable(files.MissingError("7zz isn't installed")),
                      errors.ToolError("no chrome"), broken()):
            with self.subTest(type(error).__name__):
                async def main(scheduler, fake, error=error):
                    with self.assertRaises(type(error)):
                        await scheduler.run(job(work=raising(error)))
                    with self.assertRaises(type(error)):
                        await scheduler.run(job(work=raising("never")))

                run(main)

    def test_jobs_queued_when_the_run_stops_never_run(self):
        async def main(scheduler, fake):
            gate, ran = asyncio.Event(), []

            async def full():
                await gate.wait()
                raise errors.DiskError("full")

            async def queued():
                ran.append(1)

            tasks = [asyncio.ensure_future(scheduler.run(job(Kind.FILE, work=work)))
                     for work in [full] * 8 + [queued] * 3]
            for _ in range(5):
                await asyncio.sleep(0)
            gate.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            return ran, {type(result) for result in results}

        self.assertEqual(run(main), ([], {errors.DiskError}))

    def test_a_plugins_bug_or_a_failing_tool_leaves_the_run_going(self):
        for error in (errors.PluginError("bug"), files.RunError("ffmpeg failed")):
            with self.subTest(type(error).__name__):
                async def main(scheduler, fake, error=error):
                    with self.assertRaises(type(error)):
                        await scheduler.run(job(work=raising(error)))
                    return await scheduler.run(job(work=raising("next")))

                self.assertEqual(run(main), "next")

    def test_a_retried_job_goes_first(self):
        async def main(scheduler, fake):
            made = job(work=raising(network(), "ok"))
            await scheduler.run(made)
            return made.priority

        self.assertEqual(run(main), 0)

    def test_block_rounds_reset_after_600_s_without_one(self):
        async def main(scheduler, fake):
            await scheduler.run(job(work=raising(blocked(), blocked(), "ok")))
            fake.advance(600)
            await scheduler.run(job(work=raising(blocked(), "ok")))
            return [s for s in fake.slept if s > 1]

        self.assertEqual(run(main), [30, 120, 30])

    def test_long_hold_does_not_reset_block_rounds_before_600s_after_until(self):
        async def main(scheduler, fake):
            for _ in range(3):
                await scheduler.run(job(work=raising(blocked(900), "ok")))
            with self.assertRaises(errors.Blocked):
                await scheduler.run(job(work=raising(blocked(900), "ok")))
            return [s for s in fake.slept if s > 1]

        self.assertEqual(run(main), [900, 900, 900])

    def test_a_clean_answer_ends_a_run_of_429s(self):
        async def main(scheduler, fake):
            await scheduler.run(job(work=raising(blocked(5, 429), blocked(5, 429), "ok")))
            await scheduler.run(job(work=raising(blocked(5, 429), "ok")))
            return [s for s in fake.slept if s > 1]

        self.assertEqual(run(main), [5, 5, 5])

    def test_a_429_halves_its_buckets_pace_for_20_clean_requests(self):
        async def main(scheduler, fake):
            await scheduler.run(job(work=raising(blocked(5, 429), "ok")))
            starts = []
            for _ in range(22):
                begun = clock.now()
                await scheduler.run(job(work=raising("ok")))
                starts.append(clock.now() - begun)
            return starts

        gaps = run(main)
        # The retry was the first clean request; the 20th keeps the time the halved pace saved.
        self.assertEqual(gaps[:19], [1.0] * 19)
        self.assertEqual(gaps[20:], [0.5, 0.5])

    def test_a_429_hold_shows_on_the_tally_while_waiting(self):
        tally = Tally()
        seen = []

        async def main():
            with fake_clock() as fake:
                scheduler = Scheduler(pace={"exm": 0.0}, tally=tally)
                real_sleep = fake.sleep

                def spy_sleep(seconds, **kwargs):
                    if seconds >= 5:
                        seen.append(tally.publish(force=True).now)
                    return real_sleep(seconds, **kwargs)

                with mock.patch.object(fake, "sleep", spy_sleep):
                    try:
                        await scheduler.run(job(work=raising(blocked(5, 429), "ok")))
                    finally:
                        await scheduler.close()

        asyncio.run(main())
        self.assertEqual(seen, ["held 5 s: blocked"])
        self.assertEqual(tally.publish(force=True).now, "")

    def test_a_job_rate_limited_too_much_fails_with_blocked(self):
        async def main(scheduler, fake):
            with self.assertRaises(errors.Blocked):
                await scheduler.run(job(work=raising(*(blocked(5, 429),) * 10)))
            return [s for s in fake.slept if s > 1]

        self.assertEqual(run(main), [5, 5, 30])

    def test_concurrent_429_burst_shares_hold_and_other_bucket_runs(self):
        async def main():
            scheduler = Scheduler(pace={"exm": 0.0})
            starts = []
            seen_a = [0]

            async def work_a():
                seen_a[0] += 1
                if seen_a[0] <= 2:
                    raise errors.Blocked("blocked", wait=0.08, status=429)
                starts.append(("a", clock.now()))

            async def work_b():
                starts.append(("b", clock.now()))

            try:
                t0 = clock.now()
                await asyncio.gather(
                    scheduler.run(job(bucket="a", work=work_a)),
                    scheduler.run(job(bucket="a", work=work_a)),
                    scheduler.run(job(bucket="b", work=work_b)),
                )
            finally:
                await scheduler.close()
            return starts, t0

        starts, t0 = asyncio.run(main())
        b_time = next(t for name, t in starts if name == "b")
        a_times = [t for name, t in starts if name == "a"]
        self.assertLess(b_time - t0, 0.06)
        self.assertGreaterEqual(min(a_times) - t0, 0.06)

    def test_an_overloaded_host_halves_its_cap_once_a_round(self):
        async def main(scheduler, fake):
            gate, tries = asyncio.Event(), []

            async def work():
                tries.append(clock.now())
                if len(tries) <= 3:
                    await gate.wait()
                    raise errors.SiteError("503", status=503, retryable=True)

            tasks = [asyncio.ensure_future(scheduler.run(job(Kind.FILE, work=work)))
                     for _ in range(3)]
            await asyncio.sleep(0)
            gate.set()
            await asyncio.gather(*tasks)
            return scheduler.limits.cap("example.com").value

        self.assertEqual(run(main), 4)


class CapTest(unittest.TestCase):
    """The AIMD cap's growth: a window of 10 clean transfers, judged by Little's law."""

    @staticmethod
    def window(cap, size, seconds=1.0):
        for _ in range(10):
            cap.done(size, seconds)

    def test_a_step_stays_only_when_the_rate_rose_by_half_a_place(self):
        rows = [  # (bytes of each transfer in the window after the step, cap after it)
            (100, 10),  # 9 x 100 = 900 > 800 x (1 + 0.5 / 8) = 850: kept, and grows again
            (95, 10),  # 855: kept
            (94, 8),  # 846: taken back
        ]
        for size, after in rows:
            with self.subTest(size=size):
                cap = Cap(8, most=32)
                self.window(cap, 100)
                self.assertEqual(cap.value, 9)
                self.window(cap, size)
                self.assertEqual(cap.value, after)

    def test_a_step_taken_back_holds_the_cap_for_100_transfers(self):
        cap = Cap(8, most=32)
        self.window(cap, 100)
        self.window(cap, 50)
        values = []
        for _ in range(10):
            self.window(cap, 100)
            values.append(cap.value)
        self.assertEqual(values, [8] * 9 + [9])

    def test_a_window_with_a_free_place_and_nobody_waiting_is_dropped(self):
        async def main():
            cap = Cap(8, most=32)
            await cap.take()
            cap.give()
            self.window(cap, 100)
            return cap.value

        self.assertEqual(asyncio.run(main()), 8)


class HoldTest(unittest.TestCase):
    """What is held while a job waits: its bucket for a block, its whole plugin for a wall."""

    def held(self, error, *, bucket="graphql"):
        seen = []

        class Watched(clock.Fake):
            def sleep(self, seconds, *, stop=None):
                if seconds >= 1:
                    seen.append({(h.plugin, h.bucket) for h in scheduler.holds})
                return super().sleep(seconds, stop=stop)

        async def main():
            with Scheduler() as made:
                nonlocal scheduler
                scheduler = made
                await made.run(job(bucket=bucket, work=raising(error, "ok")))

        scheduler = None
        with clock.use(Watched()):
            asyncio.run(main())
        return seen

    def test_a_block_holds_its_bucket_and_a_wall_its_plugin(self):
        rows = [
            (blocked(60, 429), [{("ex", "graphql")}]),
            (blocked(), [{("ex", "graphql")}]),
            (wall(), [{("ex", None)}]),
        ]
        for error, expected in rows:
            with self.subTest(str(error)):
                self.assertEqual(self.held(error), expected)

    def test_what_each_hold_keeps_waiting(self):
        rows = [  # (held bucket, job kind, its plugin, its bucket, when it starts)
            (None, Kind.PAGE, "ex", "graphql", 100.0),
            (None, Kind.TAB, "ex", "", 100.0),
            (None, Kind.CALL, "ex", "", 100.0),
            (None, Kind.FILE, "ex", "", 0.0),
            (None, Kind.PAGE, "other", "graphql", 0.0),
            ("graphql", Kind.PAGE, "ex", "graphql", 100.0),
            ("graphql", Kind.PAGE, "ex", "search", 0.0),
            ("graphql", Kind.TAB, "ex", "graphql", 0.0),
            ("graphql", Kind.CALL, "ex", "graphql", 0.0),
        ]
        for held, kind, plugin, bucket, start in rows:
            with self.subTest(held=held, kind=kind, plugin=plugin, bucket=bucket):
                async def main(scheduler, fake, held=held, kind=kind, plugin=plugin,
                               bucket=bucket):
                    scheduler.hold("ex", seconds=100, reason="flood wait",
                                   bucket=held)
                    starts = []

                    async def work():
                        starts.append(clock.now())

                    await scheduler.run(job(kind, plugin=plugin, bucket=bucket, work=work))
                    return starts[0]

                self.assertEqual(run(main), start)

    def test_a_plugins_hold_is_a_block_round(self):
        async def main(scheduler, fake):
            ends = []
            for seconds in (10, 200, 10):
                scheduler.hold("ex", seconds=seconds, reason="flood wait")
                ends.append(scheduler.holds[0].until - clock.now())
                fake.advance(ends[-1])
            with self.assertRaises(errors.Blocked):
                scheduler.hold("ex", seconds=10, reason="flood wait")
            fake.advance(600)
            scheduler.hold("ex", seconds=10, reason="flood wait")
            return ends, scheduler.holds[0].until - clock.now()

        self.assertEqual(run(main), ([30, 200, 480], 30))

    def test_a_fourth_hold_fails_its_job_without_another_try(self):
        async def main(scheduler, fake):
            async def work():
                scheduler.hold("ex", seconds=0, reason="flood wait")

            for _ in range(3):
                await scheduler.run(job(work=work))
            with self.assertRaises(errors.Blocked):
                await scheduler.run(job(work=work))
            return [s for s in fake.slept if s > 1]

        self.assertEqual(run(main), [30, 120, 480])


# ---------------------------------------------------------------------------------------------
# Segments, rate, slowest, free space
# ---------------------------------------------------------------------------------------------


class SegmentTest(unittest.TestCase):
    def test_which_files_may_split(self):
        rows = [  # (size, headers, status, splittable)
            (16 * MIB, {"Accept-Ranges": "bytes"}, 200, True),
            (16 * MIB, {}, 206, True),
            (16 * MIB - 1, {"Accept-Ranges": "bytes"}, 200, False),
            (64 * MIB, {"Accept-Ranges": "none"}, 200, False),
            (None, {"Accept-Ranges": "bytes"}, 200, False),
        ]
        for size, headers, status, expected in rows:
            with self.subTest(size=size, headers=headers, status=status):
                self.assertEqual(Segments.splittable(size, headers, status), expected)

    def test_when_to_split(self):
        rows = [  # (name, elapsed, room, ended, parts, splits)
            ("before 2 s", 1.9, True, False, None, False),
            ("at 2 s with room", 2.0, True, False, None, True),
            ("no room", 2.0, False, False, None, False),
            ("a range under 8 MiB", 2.0, True, False, [Segment(0, 64 * MIB, 57 * MIB)], False),
            ("8 segments", 2.0, True, False,
             [Segment(i * 8 * MIB, (i + 1) * 8 * MIB) for i in range(8)], False),
        ]
        for name, elapsed, room, ended, parts, splits in rows:
            with self.subTest(name):
                plan = Segments(64 * MIB, parts=parts or [])
                made = plan.split(elapsed=elapsed, room=room, ended=ended)
                self.assertEqual(made is not None, splits)

    def test_the_largest_range_left_is_cut_in_half(self):
        plan = Segments(64 * MIB, parts=[Segment(0, 32 * MIB, 16 * MIB),
                                         Segment(32 * MIB, 64 * MIB, 4 * MIB)])
        made = plan.split(elapsed=5, room=True)
        self.assertEqual(made, Segment(50 * MIB, 64 * MIB))
        self.assertEqual(plan.parts[1], Segment(32 * MIB, 50 * MIB, 4 * MIB))

    def test_it_checks_each_second_and_when_a_segment_ends(self):
        plan = Segments(256 * MIB)
        made = [plan.split(elapsed=t, room=True, ended=ended) is not None
                for t, ended in [(2.0, False), (2.5, False), (2.6, True), (3.6, False),
                                 (3.7, False)]]
        self.assertEqual(made, [True, False, True, True, False])

    def test_a_file_takes_at_most_4_segments(self):
        plan = Segments(1 << 30)
        for second in range(2, 30):
            plan.split(elapsed=second, room=True)
        self.assertEqual(len(plan.parts), 4)
        self.assertEqual(sum(part.end - part.start for part in plan.parts), 1 << 30)

    def test_a_fetch_file_takes_at_most_4_segments(self):
        plan = Segments(1 << 30, fetch=True)
        for second in range(2, 30):
            plan.split(elapsed=second, room=True)
        self.assertEqual(len(plan.parts), 4)

    def test_a_mirror_serves_the_same_file_only_by_length_and_etag(self):
        first = {"Content-Length": "100", "ETag": '"v1"'}
        rows = [
            ({"content-length": "100", "etag": '"v1"'}, True),
            ({"Content-Length": "100", "ETag": '"v2"'}, False),
            ({"Content-Length": "99", "ETag": '"v1"'}, False),
        ]
        for other, same in rows:
            with self.subTest(other):
                self.assertEqual(Segments.same(first, other), same)
        self.assertFalse(Segments.same({"Content-Length": "100"}, {"Content-Length": "100"}))
        self.assertFalse(Segments.same({"ETag": '"v1"'}, {"ETag": '"v1"'}))

    def test_the_sidecar_resumes_each_segment(self):
        plan = Segments(32 * MIB, '"v1"', "Tue", [Segment(0, 16 * MIB, 4 * MIB),
                                                  Segment(16 * MIB, 32 * MIB, 8 * MIB)])
        back = Segments.of(json.loads(json.dumps(plan.meta)))
        self.assertEqual([p.at for p in back.parts], [4 * MIB, 24 * MIB])
        self.assertEqual((back.etag, back.last_modified), ('"v1"', "Tue"))
        # Segments kept out of order are read in order.
        self.assertEqual([p.at for p in Segments.of(
            {"size": 10, "segments": [[5, 10, 1], [0, 5, 2]]}).parts], [2, 6])
        for name, segments in [("none", []), ("out of the file", [[0, 20, 0]]),
                               ("a gap", [[0, 4, 4], [6, 10, 0]]),
                               ("an overlap", [[0, 6, 6], [4, 10, 0]]),
                               ("short of the end", [[0, 8, 8]]),
                               ("written past its end", [[0, 5, 6], [5, 10, 0]]),
                               ("broken", [[0, "x", 0]])]:
            with self.subTest(name):
                self.assertIsNone(Segments.of({"size": 10, "segments": segments}))
        self.assertIsNone(Segments.of({"etag": '"v1"'}))


class SpeedTest(unittest.TestCase):
    def test_slowest_averages_the_last_30_s(self):
        rows = [  # (name, bytes each second for each second, slowest, below)
            ("under 30 s is never slow", [10] * 29, 100, False),
            ("slow for 30 s", [10] * 30, 100, True),
            ("fast for 30 s", [200] * 30, 100, False),
            ("fast, then slow for 30 s", [1000] * 30 + [10] * 30, 100, True),
            ("off", [0] * 40, 0, False),
        ]
        for name, seconds, slowest, below in rows:
            with self.subTest(name), fake_clock() as fake:
                speed = Speed()
                for size in seconds:
                    fake.advance(1)
                    speed.add(size)
                self.assertEqual(speed.below(slowest), below)


class FreeSpaceTest(unittest.TestCase):
    def test_a_file_needs_its_size_the_files_in_flight_and_64_mib(self):
        rows = [  # (name, free MiB, file sizes in MiB started together, bytes done, fails)
            ("room", 100, [36], 0, False),
            ("no room", 100, [37], 0, True),
            ("two in flight", 110, [20, 26], 0, False),
            ("three in flight", 110, [20, 20, 7], 0, True),
            ("done bytes are no longer due", 110, [20, 20, 7], 5 * MIB, False),
        ]
        for name, free, sizes, done, fails in rows:
            with self.subTest(name), tempfile.TemporaryDirectory() as folder:
                got = self.start(free, sizes, done, Path(folder))
                self.assertEqual(isinstance(got, errors.DiskError), fails, got)

    def start(self, free, sizes, done, folder):
        async def main(scheduler, fake):
            gate = asyncio.Event()

            async def work():
                await gate.wait()

            made = [job(Kind.FILE, size=size * MIB, path=folder / f"{n}.part", done=done,
                        host=f"h{n}.example.com", work=work) for n, size in enumerate(sizes)]
            tasks = [asyncio.ensure_future(scheduler.run(each)) for each in made]
            for _ in range(5):
                await asyncio.sleep(0)
            gate.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            return next((r for r in results if isinstance(r, BaseException)), None)

        usage = SimpleNamespace(free=free * MIB)
        with mock.patch.object(jobs.shutil, "disk_usage", return_value=usage):
            return run(main)

    def test_the_stop_names_the_file_and_stops_the_run(self):
        async def main(scheduler, fake):
            with self.assertRaises(errors.DiskError) as raised:
                await scheduler.run(job(Kind.FILE, size=MIB, path=path, work=raising("ok")))
            with self.assertRaises(errors.DiskError):
                await scheduler.run(job(work=raising("never")))
            return str(raised.exception)

        path = Path(tempfile.gettempdir()) / "clip.mp4"
        usage = SimpleNamespace(free=10 * MIB)
        with mock.patch.object(jobs.shutil, "disk_usage", return_value=usage):
            message = run(main)
        self.assertEqual(message, f"not enough free space for {path}: 65.0 MB needed, "
                                  "10.0 MB free")

    def test_free_space_is_read_once_a_second_per_device(self):
        async def main(scheduler, fake):
            for _ in range(3):
                await scheduler.run(job(Kind.FILE, size=1, path=path, work=raising("ok")))
            fake.advance(1)
            await scheduler.run(job(Kind.FILE, size=1, path=path, work=raising("ok")))

        path = Path(tempfile.gettempdir()) / "clip.mp4"
        usage = SimpleNamespace(free=1 << 40)
        with mock.patch.object(jobs.shutil, "disk_usage", return_value=usage) as read:
            run(main)
        self.assertEqual(read.call_count, 2)


if __name__ == "__main__":
    unittest.main()
