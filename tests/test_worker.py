"""Tests for `shared.worker.Pool`: threads, coroutines, backpressure, stop and the owner thread."""

from __future__ import annotations

import asyncio
import gc
import threading
import unittest
import warnings

import shared
from shared import _worker as worker
from shared import clock

WAIT = 5.0


def square(n: int) -> int:
    return n * n


async def async_square(n: int) -> int:
    await asyncio.sleep(0)
    return n * n


class Row:
    """A progress sink that records what the pool tells it."""

    def __init__(self) -> None:
        self.total: float | None = None
        self.count = 0.0

    def advance(self, n: float) -> None:
        self.count += n


class ResultsTest(unittest.TestCase):
    def test_results_arrive_as_each_finishes(self) -> None:
        release = threading.Event()

        def slow(n: int) -> int:
            release.wait(WAIT)
            return n

        with worker.Pool(jobs=2) as pool:
            pool.submit(slow, 1)
            pool.submit(square, 3)
            results = pool.results
            first = next(results)
            release.set()
            rest = list(results)
        self.assertEqual(first.value, 9)
        self.assertEqual([r.item for r in rest], [1])

    def test_a_failure_is_a_result(self) -> None:
        with worker.Pool(jobs=3) as pool:
            for n in (1, "x", 3):
                pool.submit(square, n)
            results = {r.item: r for r in pool.results}
        self.assertEqual(results[3].value, 9)
        self.assertIsInstance(results["x"].error, TypeError)
        self.assertEqual([results[n].ok for n in (1, "x", 3)], [True, False, True])

    def test_retries_back_off_on_the_clock_then_report_the_error(self) -> None:
        tries: list[int] = []

        def flaky(_: int) -> int:
            tries.append(1)
            raise ValueError("bad")

        with clock.use(clock.Fake()) as fake, worker.Pool(retries=3) as pool:
            pool.submit(flaky, 0)
            result = next(pool.results)
        self.assertEqual((result.attempts, len(tries)), (4, 4))
        self.assertIsInstance(result.error, ValueError)
        self.assertEqual(len(fake.slept), 3)
        for slept, full in zip(fake.slept, (0.5, 1.0, 2.0)):
            self.assertTrue(full / 2 <= slept <= full)

    def test_backoff_is_capped(self) -> None:
        self.assertLessEqual(max(worker._backoff(n) for n in range(1, 30)), worker.MAX_BACKOFF)

    def test_success_on_a_later_try(self) -> None:
        tries: list[int] = []

        def once(_: int) -> int:
            tries.append(1)
            if len(tries) == 1:
                raise OSError("down")
            return 7

        with clock.use(clock.Fake()), worker.Pool(retries=1) as pool:
            pool.submit(once, 0)
            result = next(pool.results)
        self.assertEqual((result.value, result.attempts), (7, 2))

    def test_progress_sink_done_and_total(self) -> None:
        row = Row()
        with worker.Pool(jobs=2, progress=row) as pool:
            for n in range(5):
                pool.submit(square, n)
            self.assertEqual(row.total, 5)
            self.assertEqual(sorted(r.value for r in pool.results), [0, 1, 4, 9, 16])
            self.assertEqual((pool.done, pool.total), (5, 5))
        self.assertEqual(row.count, 5)

    def test_bad_options(self) -> None:
        for options in ({"jobs": 0}, {"jobs": "many"}, {"jobs": True}, {"retries": -1}):
            with self.subTest(options), self.assertRaises(ValueError):
                worker.Pool(**options)  # type: ignore[arg-type]

    def test_named_job_counts(self) -> None:
        self.assertGreaterEqual(worker.Pool(jobs="cpu")._jobs, 1)
        self.assertLessEqual(worker.Pool(jobs="io")._jobs, 32)

    def test_error_hierarchy(self) -> None:
        self.assertTrue(issubclass(worker.Error, shared.Error))
        self.assertTrue(issubclass(worker.ClosedError, RuntimeError))


class BackpressureTest(unittest.TestCase):
    def test_submit_blocks_at_twice_the_jobs(self) -> None:
        release = threading.Event()
        submitted: list[int] = []
        pool = worker.Pool(jobs=2)

        def feed() -> None:
            for n in range(5):
                pool.submit(release.wait, WAIT)
                submitted.append(n)

        feeder = threading.Thread(target=feed)
        feeder.start()
        for _ in range(50):
            if len(submitted) == 4:
                break
            threading.Event().wait(0.02)
        threading.Event().wait(0.1)
        self.assertEqual(len(submitted), 4)
        release.set()
        feeder.join(WAIT)
        self.assertEqual(len(submitted), 5)
        pool.close()

    def test_many_submissions_keep_memory_flat(self) -> None:
        jobs, peak = 4, [0]

        def note(_: int) -> None:
            peak[0] = max(peak[0], len(pool._queue) + pool._running)

        with worker.Pool(jobs=jobs) as pool:
            for n in range(100_000):
                pool.submit(note, n)
                if n % 1000 == 999:
                    while pool._finished:
                        next(pool.results)
            consumed = sum(1 for _ in pool.results)
            self.assertLessEqual(consumed, 1000 + jobs * 2)
        self.assertLessEqual(peak[0], jobs * 2 + 1)
        self.assertEqual((pool.done, len(pool._finished), len(pool._queue)), (100_000, 0, 0))


class StopTest(unittest.TestCase):
    def test_stop_cancels_queued_tasks(self) -> None:
        started, release = threading.Event(), threading.Event()
        ran: list[str] = []

        def slow() -> str:
            started.set()
            release.wait(WAIT)
            ran.append("slow")
            return "slow"

        pool = worker.Pool(jobs=1)
        pool.submit(slow)
        pool.submit(ran.append, "queued")
        self.assertTrue(started.wait(WAIT))
        pool.stop()
        self.assertTrue(pool.stopping)
        release.set()
        results = {repr(r.item): r for r in pool.results}
        self.assertEqual(ran, ["slow"])
        self.assertIsInstance(results["'queued'"].error, InterruptedError)
        with self.assertRaises(worker.ClosedError):
            pool.submit(square, 1)
        pool.close()

    def test_the_stop_event_cancels_queued_tasks(self) -> None:
        event, release = threading.Event(), threading.Event()
        pool = worker.Pool(jobs=1, stop=event)
        pool.submit(release.wait, WAIT)
        pool.submit(square, 2)
        event.set()
        release.set()
        results = list(pool.results)
        pool.close()
        self.assertTrue(pool.stopping)
        self.assertTrue(any(isinstance(r.error, InterruptedError) for r in results))

    def test_stop_never_sets_the_callers_event(self) -> None:
        event = threading.Event()
        with worker.Pool(stop=event) as pool:
            pool.stop()
        self.assertFalse(event.is_set())

    def test_close_then_submit_raises_and_close_twice_is_fine(self) -> None:
        pool = worker.Pool()
        pool.close()
        pool.close()
        with self.assertRaises(worker.ClosedError):
            pool.submit(square, 1)
        self.assertEqual(list(pool.results), [])

    def _interrupted_block_keeps_queue(self, error: type[BaseException]) -> None:
        started, release = threading.Event(), threading.Event()
        ran: list[int] = []

        def slow(n: int) -> None:
            started.set()
            release.wait(WAIT)
            ran.append(n)

        timer = threading.Timer(0.1, release.set)
        timer.start()
        with self.assertRaises(error):
            with worker.Pool(jobs=1) as pool:
                for n in range(2):  # one runs, one waits in the queue
                    pool.submit(slow, n)
                self.assertTrue(started.wait(WAIT))
                raise error()
        timer.cancel()
        self.assertEqual(ran, [0])
        self.assertEqual(pool.done, 2)

    def test_an_exception_in_the_block_does_not_drain_the_queue(self) -> None:
        self._interrupted_block_keeps_queue(RuntimeError)

    def test_ctrl_c_in_the_block_does_not_drain_the_queue(self) -> None:
        self._interrupted_block_keeps_queue(KeyboardInterrupt)


class PauseTest(unittest.TestCase):
    def test_no_task_starts_while_paused(self) -> None:
        ran = threading.Event()
        with worker.Pool(jobs=1) as pool:
            pool.pause()
            pool.submit(ran.set)
            self.assertFalse(ran.wait(0.2))
            pool.resume()
            self.assertTrue(ran.wait(WAIT))

    def test_closing_a_paused_pool_finishes_its_queue(self) -> None:
        pool = worker.Pool(jobs=1)
        pool.pause()
        pool.submit(square, 4)
        pool.close()
        self.assertEqual(next(pool.results).value, 16)


class AsyncTest(unittest.TestCase):
    def test_coroutine_functions_run_on_one_loop_thread(self) -> None:
        threads: set[int] = set()

        async def where(n: int) -> int:
            threads.add(threading.get_ident())
            return await async_square(n)

        with worker.Pool(jobs=3) as pool:
            self.assertIsNone(pool._loop)
            for n in range(4):
                pool.submit(where, n)
            pool.submit(square, 5)
            values = sorted(r.value for r in pool.results)
        self.assertEqual(values, [0, 1, 4, 9, 25])
        self.assertEqual(len(threads), 1)
        self.assertNotIn(threading.get_ident(), threads)

    def test_wait_returns_the_value_and_raises_the_error(self) -> None:
        async def fail() -> None:
            raise ValueError("bad")

        with worker.Pool() as pool:
            self.assertEqual(pool.wait(async_square, 4), 16)
            with self.assertRaisesRegex(ValueError, "bad"):
                pool.wait(fail)
            self.assertEqual(pool.wait(async_square, 5), 25)
        with self.assertRaises(worker.ClosedError):
            pool.wait(async_square, 1)

    def test_close_cancels_pending_loop_tasks_without_warnings(self) -> None:
        cancelled = threading.Event()

        async def forever() -> None:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async def spawn() -> None:
            asyncio.get_running_loop().create_task(forever())
            await asyncio.sleep(0)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with worker.Pool() as pool:
                pool.wait(spawn)
            loop = pool._loop
            gc.collect()
        self.assertTrue(cancelled.is_set())
        self.assertIsNotNone(loop)
        assert loop is not None
        self.assertTrue(loop.is_closed())
        self.assertEqual([str(w.message) for w in caught], [])


class CallTest(unittest.TestCase):
    def test_call_runs_on_the_owner_thread(self) -> None:
        owner = threading.get_ident()
        with worker.Pool(jobs=4) as pool:
            for _ in range(8):
                pool.submit(pool.call, threading.get_ident)
            seen = {r.value for r in pool.results}
        self.assertEqual(seen, {owner})

    def test_call_from_the_owner_runs_in_place(self) -> None:
        with worker.Pool() as pool:
            self.assertEqual(pool.call(threading.get_ident), threading.get_ident())

    def test_calls_are_answered_while_submit_blocks_and_on_exit(self) -> None:
        owner = threading.get_ident()
        with worker.Pool(jobs=1) as pool:
            for _ in range(5):
                pool.submit(pool.call, threading.get_ident)
        self.assertEqual(pool.done, 5)
        self.assertEqual({r.value for r in pool.results}, {owner})

    def test_call_after_close_raises(self) -> None:
        pool = worker.Pool()
        pool.close()
        errors: list[BaseException] = []

        def other() -> None:
            try:
                pool.call(lambda: None)
            except worker.ClosedError as error:
                errors.append(error)

        thread = threading.Thread(target=other)
        thread.start()
        thread.join(WAIT)
        self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()
