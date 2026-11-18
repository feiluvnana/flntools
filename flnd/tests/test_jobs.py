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

from flnd import errors
from flnd.engine import jobs
from flnd.engine.jobs import (
    Backlog,
    Cap,
    Job,
    Kind,
    Pool,
    Rate,
    Scheduler,
    Segment,
    Segments,
    Speed,
)
from flnd.testing import fake_clock
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
    fields.setdefault("account", "example_user")
    fields.setdefault("host", "example.com")
    return Job(kind=kind, work=fields.pop("work", None), **fields)


class Hidden(errors.SiteError):
    """`flnd.errors.Hidden`, which plugin API 2 adds."""


class PoolTest(unittest.TestCase):
    def test_each_pools_numbers(self):
        rows = [  # (cores, kind, places)
            (8, Kind.CALL, 12), (64, Kind.CALL, 32), (8, Kind.HASH, 8), (8, Kind.UNPACK, 1),
            (8, Kind.MERGE, 4), (1, Kind.MERGE, 1), (8, Kind.EXEC, 1),
        ]
        for cpus, kind, size in rows:
            with self.subTest(cpus=cpus, kind=kind):
                self.assertEqual(run(lambda s, f, k=kind: _size(s, k), cpus=cpus), size)

    def test_how_many_run_at_once(self):
        def tabs(n, accounts=1, scroll=False):
            return [job(Kind.TAB, account=f"a{i % accounts}", scroll=scroll) for i in range(n)]

        rows = {  # name: (jobs, options, most at once)
            "merge lane": ([job(Kind.MERGE) for _ in range(9)], {}, 4),
            "exec lane": ([job(Kind.EXEC) for _ in range(3)], {}, 1),
            "tab budget": (tabs(9), {}, 6),
            "scrolling tabs of one account": (tabs(5, scroll=True), {}, 2),
            "scrolling tabs of two accounts": (tabs(6, 2, scroll=True), {}, 4),
            "a host's cap starts at 8": ([job(Kind.FILE) for _ in range(20)], {}, 8),
            "a host's own limit": (
                [job(Kind.FILE, host="cdn.example.com") for _ in range(9)],
                {"limits": {"example.com": 3}}, 3),
            "places per target": (
                [job(Kind.FILE, host=f"h{i}.example.com", target="t") for i in range(40)],
                {}, 32),
            "places follow the largest limit": (
                [job(Kind.FILE, host=f"h{i}.example.com", target="t") for i in range(50)],
                {"limits": {"other.example": 40}}, 40),
        }
        for name, (made, options, most) in rows.items():
            with self.subTest(name):
                self.assertEqual(run(lambda s, f, m=made: peak(s, m), **options), most)

    def test_a_targets_queue_holds_four_jobs_a_place(self):
        async def main(scheduler, fake):
            queue, places = scheduler._target("t")
            return queue.size, places.size, scheduler.limits.cap("x.example.com").most

        self.assertEqual(run(main), (128, 32, 32))

    def test_a_warm_tab_opens_only_with_two_tabs_free(self):
        async def main(scheduler, fake):
            gate = asyncio.Event()
            seen = []

            async def work():
                seen.append(scheduler.warm)
                await gate.wait()

            tasks = [asyncio.ensure_future(scheduler.run(job(Kind.TAB, work=work)))
                     for _ in range(5)]
            for _ in range(3):
                await asyncio.sleep(0)
            seen.append(scheduler.warm)
            gate.set()
            await asyncio.gather(*tasks)
            return seen

        self.assertEqual(run(main), [True, True, True, True, False, False])

    def test_exec_runs_in_the_order_files_were_saved(self):
        async def main(scheduler, fake):
            order = []

            def work(n):
                async def done():
                    order.append(n)
                    await asyncio.sleep(0)
                return done

            await asyncio.gather(*[scheduler.run(job(Kind.EXEC, work=work(n))) for n in range(6)])
            return order

        self.assertEqual(run(main), list(range(6)))

    def test_a_tab_waiting_to_scroll_holds_no_tab(self):
        async def main(scheduler, fake):
            gate, started = asyncio.Event(), []

            async def scroll():
                await gate.wait()

            async def other():
                started.append(scheduler.tabs.busy)

            tasks = [asyncio.ensure_future(scheduler.run(job(Kind.TAB, scroll=True, work=scroll)))
                     for _ in range(6)]
            tasks.append(asyncio.ensure_future(scheduler.run(
                job(Kind.TAB, account="b", work=other))))
            for _ in range(5):
                await asyncio.sleep(0)
            gate.set()
            await asyncio.gather(*tasks)
            return started

        self.assertEqual(run(main), [3])

    def test_a_retried_exec_keeps_the_order_of_the_saves(self):
        async def main(scheduler, fake):
            calls = []

            def work(n, outcomes):
                async def done():
                    calls.append(n)
                    await asyncio.sleep(0)
                    if outcomes:
                        raise outcomes.pop(0)
                return done

            made = [job(Kind.EXEC, work=work(n, [network()] if n == 1 else []))
                    for n in range(3)]
            await asyncio.gather(*[scheduler.run(each) for each in made])
            return calls, [each.priority for each in made]

        self.assertEqual(run(main), ([0, 1, 1, 2], [3, 3, 3]))

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

    def test_kinds_take_their_priorities(self):
        expected = {Kind.PAGE: 1, Kind.TAB: 1, Kind.FILE: 2, Kind.HASH: 3, Kind.UNPACK: 3,
                    Kind.MERGE: 3, Kind.EXEC: 3}
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

    def test_four_files_hashed_at_once_use_four_threads(self):
        """K9: one hasher thread made four hashes wait in turn."""
        barrier = threading.Barrier(4, timeout=2)

        def digest():
            barrier.wait()
            return threading.get_ident()

        async def main(scheduler, fake):
            async def work():
                return await scheduler.on(Kind.HASH, digest)

            return await asyncio.gather(*[scheduler.run(job(Kind.HASH, work=work))
                                          for _ in range(4)])

        self.assertEqual(len(set(run(main, cpus=4))), 4)

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

    def test_a_transfer_pauses_at_8_mib_unwritten_and_resumes_at_4(self):
        async def main():
            backlog, pieces, paused = Backlog(), [], []
            for _ in range(9):
                piece = backlog.add(b"x" * MIB)
                if piece:
                    pieces.append(len(piece))
                paused.append(backlog.paused)
            backlog.drain(4 * MIB)
            paused.append(backlog.paused)
            backlog.drain(1 * MIB)
            paused.append(backlog.paused)
            return pieces, paused

        pieces, paused = asyncio.run(main())
        self.assertEqual(pieces, [4 * MIB, 4 * MIB])
        self.assertEqual(paused, [False] * 7 + [True, True, True, False])

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
    error = errors.SourceError("the class declares no domains")
    error.broken = True
    return error


def network():
    return errors.NetworkError("reset", retryable=True)


def moved(made, error):
    """A switch hook with another account always free."""
    other = Job(kind=made.kind, work=raising("on b"), plugin=made.plugin, account="b",
                bucket=made.bucket, host=made.host, first=made.first)
    return other


class RetryTest(unittest.TestCase):
    def outcome(self, *outcomes, kind=Kind.PAGE, first=False, **options):
        """What a job with these outcomes ends with, the waits it slept, and the scheduler."""
        async def main(scheduler, fake):
            try:
                result = await scheduler.run(job(kind, first=first, work=raising(*outcomes)))
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
            "a block moves with switching": ((blocked(),), {"switch": moved}, "on b", []),
            "a wall holds 30 s then 120 s": ((wall(), wall(), "ok"), {}, "ok", [30, 120]),
            "a third wall fails": ((wall(), wall(), wall()), {}, errors.LoginError, [30, 120]),
            "a third wall moves with switching": (
                (wall(), wall(), wall()), {"switch": moved}, "on b", [30, 120]),
            "a 401 fails at once": ((errors.LoginError("401"),), {}, errors.LoginError, []),
            "a 401 moves with switching": (
                (errors.LoginError("401"),), {"switch": moved}, "on b", []),
            "hidden fails": ((Hidden("hidden"),), {}, Hidden, []),
            "hidden moves with switching": ((Hidden("hidden"),), {"switch": moved}, "on b", []),
            "not found fails": ((errors.NotFound("gone"),), {}, errors.NotFound, []),
            "not found past the first page fails with switching": (
                (errors.NotFound("gone"),), {"switch": moved}, errors.NotFound, []),
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
                (errors.Error("busy", retryable=True), "ok"), {"kind": Kind.HASH}, "ok", [1.0]),
            "a missing tool is never retried": (
                (retryable(files.MissingError("ffmpeg isn't installed")), "ok"), {},
                files.MissingError, []),
            "a plugin's bug fails its job": (
                (errors.SourceError("bug"), "ok"), {}, errors.SourceError, []),
            "a broken plugin class fails": ((broken(),), {}, errors.SourceError, []),
        }
        with mock.patch.object(jobs, "_HIDDEN", (Hidden,)), \
                mock.patch.object(jobs.random, "uniform", return_value=1.0):
            for name, (outcomes, options, result, waits) in rows.items():
                with self.subTest(name):
                    got, slept, _ = self.outcome(*outcomes, **options)
                    self.assertEqual(got, result)
                    self.assertEqual(slept, waits)

    def test_not_found_on_a_first_page_moves_with_switching(self):
        for kind in (Kind.PAGE, Kind.TAB):
            with self.subTest(kind):
                got, _, _ = self.outcome(errors.NotFound("gone"), kind=kind, first=True,
                                         switch=moved)
                self.assertEqual(got, "on b")

    def test_network_failures_and_5xx_wait_1_2_4_8_s_with_jitter(self):
        for error in (network(), errors.SiteError("502", status=502, retryable=True)):
            for tries, result in ((4, "ok"), (5, type(error))):
                with self.subTest(error=str(error), tries=tries):
                    got, slept, _ = self.outcome(*[error] * tries, "ok")
                    self.assertEqual(got, result)
                    self.assertEqual(len(slept), 4)
                    for wait, base in zip(slept, (1, 2, 4, 8), strict=True):
                        self.assertTrue(base * 0.5 <= wait <= base * 1.5, (wait, base))

    def test_a_login_failure_marks_the_account_expired(self):
        _, _, scheduler = self.outcome(errors.LoginError("403"))
        self.assertEqual(scheduler.expired, {("ex", "example_user")})
        _, _, scheduler = self.outcome(errors.LoginError("403"), switch=moved)
        self.assertEqual(scheduler.expired, set())

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

    def test_a_moved_job_starts_its_tries_afresh(self):
        def switch(made, error):
            if made.account == "b":
                return None
            return Job(kind=made.kind, work=raising(wall(), wall(), wall()), plugin=made.plugin,
                       account="b", host=made.host)

        got, slept, _ = self.outcome(wall(), wall(), wall(), switch=switch)
        self.assertEqual((got, slept), (errors.LoginError, [30, 120, 30, 120]))

    def test_a_plugins_bug_or_a_failing_tool_leaves_the_run_going(self):
        for error in (errors.SourceError("bug"), files.RunError("ffmpeg failed")):
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
    """What is held while a job waits: its bucket for a block, its whole account for a wall."""

    def held(self, error, *, bucket="graphql"):
        seen = []

        class Watched(clock.Fake):
            def sleep(self, seconds, *, stop=None):
                if seconds >= 1:
                    seen.append({(h.account, h.bucket) for h in scheduler.holds})
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

    def test_a_block_holds_its_bucket_and_a_wall_its_account(self):
        rows = [
            (blocked(60, 429), [{("example_user", "graphql")}]),
            (blocked(), [{("example_user", "graphql")}]),
            (wall(), [{("example_user", None)}]),
        ]
        for error, expected in rows:
            with self.subTest(str(error)):
                self.assertEqual(self.held(error), expected)

    def test_what_each_hold_keeps_waiting(self):
        rows = [  # (held bucket, job kind, its account, its bucket, when it starts)
            (None, Kind.PAGE, "example_user", "graphql", 100.0),
            (None, Kind.TAB, "example_user", "", 100.0),
            (None, Kind.CALL, "example_user", "", 100.0),
            (None, Kind.FILE, "example_user", "", 0.0),
            (None, Kind.PAGE, "b", "graphql", 0.0),
            ("graphql", Kind.PAGE, "example_user", "graphql", 100.0),
            ("graphql", Kind.PAGE, "example_user", "search", 0.0),
            ("graphql", Kind.TAB, "example_user", "graphql", 0.0),
            ("graphql", Kind.CALL, "example_user", "graphql", 0.0),
        ]
        for held, kind, account, bucket, start in rows:
            with self.subTest(held=held, kind=kind, account=account, bucket=bucket):
                async def main(scheduler, fake, held=held, kind=kind, account=account,
                               bucket=bucket):
                    scheduler.hold("ex", "example_user", seconds=100, reason="flood wait",
                                   bucket=held)
                    starts = []

                    async def work():
                        starts.append(clock.now())

                    await scheduler.run(job(kind, account=account, bucket=bucket, work=work))
                    return starts[0]

                self.assertEqual(run(main), start)

    def test_a_plugins_hold_is_a_block_round(self):
        async def main(scheduler, fake):
            ends = []
            for seconds in (10, 200, 10):
                scheduler.hold("ex", "example_user", seconds=seconds, reason="flood wait")
                ends.append(scheduler.holds[0].until - clock.now())
                fake.advance(ends[-1])
            with self.assertRaises(errors.Blocked):
                scheduler.hold("ex", "example_user", seconds=10, reason="flood wait")
            fake.advance(600)
            scheduler.hold("ex", "example_user", seconds=10, reason="flood wait")
            return ends, scheduler.holds[0].until - clock.now()

        self.assertEqual(run(main), ([30, 200, 480], 30))

    def test_a_fourth_hold_fails_its_job_without_another_try(self):
        async def main(scheduler, fake):
            async def work():
                scheduler.hold("ex", "example_user", seconds=0, reason="flood wait")

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

    def test_a_file_takes_at_most_8_segments(self):
        plan = Segments(1 << 30)
        for second in range(2, 30):
            plan.split(elapsed=second, room=True)
        self.assertEqual(len(plan.parts), 8)
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


class RateTest(unittest.TestCase):
    def test_one_byte_bucket_holds_at_most_a_second(self):
        async def main():
            rate = Rate(MIB)
            await rate.take(MIB)
            first = clock.now()
            await rate.take(2 * MIB)
            return first, clock.now()

        with fake_clock():
            self.assertEqual(asyncio.run(main()), (0.0, 2.0))

    def test_a_file_job_waits_for_the_bytes_owed(self):
        async def main(scheduler, fake):
            await scheduler.rate.take(3 * MIB)
            begun = clock.now()
            await scheduler.run(job(Kind.FILE, work=raising("ok")))
            return begun, clock.now()

        self.assertEqual(run(main, rate=MIB), (2.0, 2.0))
        self.assertEqual(run(main, rate=0), (0.0, 0.0))

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
