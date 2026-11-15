"""Tests for `shared.worker`: the thread `Pool`, `map`, and the background `Loop`."""

from __future__ import annotations

import asyncio
import threading
import time
import unittest

import shared
from shared import _worker as worker
from shared import clock


def square(n: int) -> int:
    return n * n


async def async_square(n: int) -> int:
    await asyncio.sleep(0)
    return n * n


class PoolTest(unittest.TestCase):
    def test_results_come_back_in_submission_order(self) -> None:
        with worker.Pool(concurrency=3) as pool:
            pool.submit(square, 1)
            pool.submit(square, "x")
            pool.submit(square, 3)
            results = pool.join()
        self.assertEqual([r.item for r in results], [1, "x", 3])
        self.assertEqual([r.value for r in results], [1, None, 9])
        self.assertIsInstance(results[1].error, TypeError)
        self.assertEqual([r.ok for r in results], [True, False, True])

    def test_as_completed_yields_each_task_once(self) -> None:
        with worker.Pool(concurrency=2) as pool:
            for n in range(4):
                pool.submit(square, n)
            seen = sorted(r.value for r in pool.as_completed() if r.value is not None)
            self.assertEqual(seen, [0, 1, 4, 9])
            self.assertEqual(list(pool.as_completed()), [])

    def test_progress_runs_once_per_result_on_join(self) -> None:
        calls: list[tuple[int, int]] = []
        with worker.Pool(concurrency=1, progress=lambda d, t: calls.append((d, t))) as pool:
            pool.submit(square, 1)
            pool.submit(square, 2)
            pool.join()
        self.assertEqual(calls, [(2, 2)])

    def test_retries_then_reports_the_error(self) -> None:
        fake = clock.Fake()
        tries: list[int] = []

        def flaky(_: int) -> int:
            tries.append(1)
            raise ValueError("bad")

        with clock.use(fake), worker.Pool(retries=3) as pool:
            pool.submit(flaky, 0)
            result = pool.join()[0]
        self.assertEqual(result.attempts, 4)
        self.assertEqual(len(tries), 4)
        self.assertIsInstance(result.error, ValueError)
        self.assertEqual(fake.slept, [0.5, 1.0, 2.0])

    def test_success_on_a_later_try(self) -> None:
        tries: list[int] = []

        def once(_: int) -> int:
            tries.append(1)
            if len(tries) == 1:
                raise OSError("down")
            return 7

        with worker.Pool(retries=1) as pool:
            pool.submit(once, 0)
            result = pool.join()[0]
        self.assertEqual((result.value, result.attempts), (7, 2))

    def test_stop_drops_queued_tasks(self) -> None:
        started, release = threading.Event(), threading.Event()
        ran: list[str] = []

        def slow() -> str:
            started.set()
            release.wait(5)
            ran.append("slow")
            return "slow"

        pool = worker.Pool(concurrency=1)
        pool.submit(slow)
        pool.submit(ran.append, "queued")
        self.assertTrue(started.wait(5))
        pool.stop()
        release.set()
        results = pool.join()
        self.assertEqual(ran, ["slow"])
        self.assertFalse(results[1].ok)
        self.assertIsInstance(results[1].error, InterruptedError)
        with self.assertRaises(worker.ClosedError):
            pool.submit(square, 1)
        pool.close()

    def test_close_then_submit_raises_and_close_twice_is_fine(self) -> None:
        pool = worker.Pool()
        pool.close()
        pool.close()
        with self.assertRaises(worker.ClosedError) as caught:
            pool.submit(square, 1)
        self.assertIsInstance(caught.exception, RuntimeError)
        self.assertEqual(pool.join(), [])

    def test_join_timeout_raises_stalled(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        pool = worker.Pool(concurrency=1, timeout=0.05)
        pool.submit(release.wait, 5)
        with self.assertRaises(worker.StalledError):
            pool.join()
        pool.close()

    def test_bad_options(self) -> None:
        for options in ({"concurrency": 0}, {"retries": -1}):
            with self.subTest(options), self.assertRaises(ValueError):
                worker.Pool(**options)  # type: ignore[arg-type]

    def test_error_hierarchy(self) -> None:
        self.assertTrue(issubclass(worker.Error, shared.Error))
        self.assertTrue(issubclass(worker.ClosedError, RuntimeError))
        self.assertTrue(issubclass(worker.StalledError, TimeoutError))

    def test_map(self) -> None:
        results = worker.map(square, [1, 2, 3], concurrency=2)
        self.assertEqual([r.value for r in results], [1, 4, 9])


class LoopTest(unittest.TestCase):
    def test_runs_coroutines_and_survives_task_errors(self) -> None:
        async def fail() -> None:
            raise ValueError("bad")

        with worker.Loop() as loop:
            self.assertIsNotNone(loop.raw)
            self.assertEqual(loop.run(async_square(4)), 16)
            with self.assertRaisesRegex(ValueError, "bad"):
                loop.run(fail())
            self.assertEqual(loop.run(async_square(5)), 25)
        self.assertIsNone(loop.raw)

    def test_timeout_raises_stalled_and_closed_raises(self) -> None:
        async def wait_forever() -> None:
            await asyncio.sleep(5)

        with worker.Loop() as loop:
            with self.assertRaises(worker.StalledError):
                loop.run(wait_forever(), timeout=0.05)
        with self.assertRaises(worker.ClosedError):
            loop.run(async_square(1))


if __name__ == "__main__":
    unittest.main()
