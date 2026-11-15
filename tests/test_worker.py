"""Tests for `shared.worker`: `Pool` and `Loop` over threads and coroutines."""

from __future__ import annotations

import _thread
import asyncio
import concurrent.futures
import gc
import threading
import time
import unittest
from io import StringIO
from unittest import mock

import shared
from shared import _worker, clock, worker
from shared._ui import ui

LONG = 10.0
STOPPING = "! stopping: waiting for 1 running tasks (Ctrl-C again to force)"


def square(n: int) -> int:
    return n * n


async def async_square(n: int) -> int:
    await asyncio.sleep(0)
    return n * n


def wait_until(condition, limit: float = LONG) -> None:
    deadline = time.monotonic() + limit
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.005)


class WorkerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dest = StringIO()
        self.scope = ui.scope(dest=self.dest, color=False)
        self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)

    def lines(self) -> list[str]:
        return self.dest.getvalue().splitlines()

    def after_line(self, text: str, action) -> None:
        """Run `action` in a thread once `text` is written to `self.dest`."""
        def watch() -> None:
            wait_until(lambda: text in self.dest.getvalue())
            action()

        threading.Thread(target=watch, daemon=True).start()


class PoolAsCompletedTest(WorkerCase):
    def test_every_result_in_finish_order_and_never_raises(self) -> None:
        gates = {n: threading.Event() for n in range(3)}

        def run(n: int) -> int:
            gates[n].wait(LONG)
            if n == 1:
                raise ValueError("bad")
            return n

        with worker.Pool(concurrency=3) as pool:
            for n in (0, 1, 2):
                pool.submit(run, n)
            results = pool.as_completed()
            order = []
            for n in (2, 0, 1):
                gates[n].set()
                result = next(results)
                order.append((result.item, result.ok))
            self.assertEqual(order, [(2, True), (0, True), (1, False)])
            self.assertEqual(list(results), [])

    def test_result_fields_and_early_break(self) -> None:
        with worker.Pool(concurrency=1) as pool:
            pool.submit(square, 3)
            (result,) = list(pool.as_completed())
            self.assertEqual(
                (result.item, result.value, result.error, result.ok, result.attempts),
                (3, 9, None, True, 1),
            )

        ran = []
        with worker.Pool(concurrency=1) as pool:
            for n in range(20):
                pool.submit(ran.append, n)
            for _ in pool.as_completed():
                pool.stop()
                break
        self.assertLess(len(ran), 20)


class PoolTest(WorkerCase):
    def test_surface_omits_removed_symbols(self) -> None:
        self.assertFalse(hasattr(worker, "map"))
        self.assertFalse(hasattr(worker, "as_completed"))
        self.assertFalse(hasattr(worker, "TaskError"))
        self.assertTrue(issubclass(worker.Error, shared.Error))
        pool = worker.Pool()
        self.assertFalse(hasattr(pool, "results"))
        self.assertFalse(hasattr(pool, "submitted"))
        self.assertIsInstance(pool.raw, concurrent.futures.Executor)
        pool.close()
        pool.join()

    def test_submit_while_tasks_run(self) -> None:
        first_started, release = threading.Event(), threading.Event()

        def first() -> str:
            first_started.set()
            release.wait(LONG)
            return "first"

        with worker.Pool(concurrency=2) as pool:
            pool.submit(first)
            self.assertTrue(first_started.wait(LONG))
            pool.submit(square, 4)
            release.set()
            results = pool.join()
        self.assertEqual(sorted(str(result.value) for result in results), ["16", "first"])

    def test_submit_waits_when_the_queue_is_full(self) -> None:
        release = threading.Event()
        submitted = []
        with worker.Pool(concurrency=1) as pool:
            def produce() -> None:
                for n in range(5):
                    pool.submit(release.wait, LONG)
                    submitted.append(n)

            producer = threading.Thread(target=produce)
            producer.start()
            wait_until(lambda: len(submitted) >= 3)
            producer.join(0.1)
            self.assertTrue(producer.is_alive())
            self.assertEqual(len(submitted), 3)
            release.set()
            producer.join(LONG)
            results = pool.join()
        self.assertEqual(len(results), 5)

    def test_failures_are_results(self) -> None:
        with worker.Pool(concurrency=2) as pool:
            pool.submit(square, 2)
            pool.submit(square, "x")
            results = pool.join()
        self.assertEqual(sorted(result.ok for result in results), [False, True])

    def test_close_then_submit_raises_and_close_twice_is_fine(self) -> None:
        pool = worker.Pool()
        pool.close()
        pool.close()
        with self.assertRaises(worker.ClosedError) as caught:
            pool.submit(square, 1)
        self.assertIsInstance(caught.exception, RuntimeError)
        self.assertEqual(pool.join(), [])

    def test_join_before_close_then_submit_more(self) -> None:
        pool = worker.Pool(concurrency=2)
        pool.submit(square, 1)
        pool.submit(square, 2)
        self.assertEqual(sorted(result.value for result in pool.join()), [1, 4])
        pool.submit(square, 3)
        pool.close()
        self.assertEqual(sorted(result.value for result in pool.join()), [1, 4, 9])

    def test_join_timeout_returns_what_finished(self) -> None:
        release = threading.Event()
        pool = worker.Pool(concurrency=2)
        pool.submit(square, 5)
        pool.submit(release.wait, LONG)
        wait_until(lambda: len(pool._run.finished) == 1)
        self.assertEqual([result.value for result in pool.join(timeout=0.05)], [25])
        release.set()
        pool.close()
        self.assertEqual(len(pool.join()), 2)
        self.assertEqual(self.lines(), [])

    def test_warning_when_never_joined(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        pool = worker.Pool()
        pool.submit(release.wait, LONG)
        del pool
        gc.collect()
        self.assertEqual(
            self.lines(), ["! worker.Pool was never joined; 1 tasks may not have finished"]
        )

    def test_unused_pool_is_silent(self) -> None:
        pool = worker.Pool()
        del pool
        gc.collect()
        self.assertEqual(self.lines(), [])

    def test_block_error_drops_queued_tasks_with_a_line(self) -> None:
        started, release = threading.Event(), threading.Event()
        ran = []

        def slow() -> None:
            started.set()
            release.wait(LONG)
            ran.append("slow")

        with self.assertRaises(RuntimeError):
            with worker.Pool(concurrency=1) as pool:
                pool.submit(slow)
                pool.submit(ran.append, "queued")
                pool.submit(ran.append, "queued")
                started.wait(LONG)
                release.set()
                raise RuntimeError("body")
        self.assertEqual(ran, ["slow"])
        self.assertEqual(self.lines(), ["! dropped 2 queued tasks after RuntimeError"])

    def test_a_task_can_submit_to_its_own_pool(self) -> None:
        seen = []

        def outer() -> None:
            for n in range(5):
                pool.submit(seen.append, n)

        with worker.Pool(concurrency=1) as pool:
            pool.submit(outer)
            pool.join()
        self.assertEqual(seen, [0, 1, 2, 3, 4])

    def test_pool_stop_drops_queued_and_waits_for_running(self) -> None:
        started, release = threading.Event(), threading.Event()
        ran = []

        def slow() -> None:
            started.set()
            release.wait(LONG)
            ran.append("slow")

        pool = worker.Pool(concurrency=1)
        pool.submit(slow)
        pool.submit(ran.append, "queued")
        self.assertTrue(started.wait(LONG))
        release.set()
        pool.stop()
        self.assertEqual(ran, ["slow"])
        with self.assertRaises(worker.ClosedError):
            pool.submit(square, 1)

    def test_run_on_owner_marshals_to_owner_thread(self) -> None:
        owner_id = threading.get_ident()
        seen_threads = []

        def task(n: int) -> int:
            return pool.run_on_owner(
                lambda: (seen_threads.append(threading.get_ident()), n * 2)[1]
            )

        with worker.Pool(concurrency=2) as pool:
            for n in range(4):
                pool.submit(task, n)
            results = pool.join()
        self.assertEqual(seen_threads, [owner_id] * 4)
        self.assertEqual(sorted(r.value for r in results), [0, 2, 4, 6])

    def test_run_on_owner_stops_cleanly_on_pool_stop(self) -> None:
        started = threading.Event()
        errors = []

        def ask_owner() -> None:
            started.set()
            try:
                pool.run_on_owner(lambda: 1)
            except worker.ClosedError as err:
                errors.append(err)

        pool = worker.Pool(concurrency=1)
        pool.submit(ask_owner)
        self.assertTrue(started.wait(LONG))
        pool.stop()
        self.assertEqual(len(errors), 1)


class AsyncAndTimeoutTest(WorkerCase):
    def test_async_tasks_and_timeout_cancellation(self) -> None:
        active, most = [0], [0]

        async def fetch(n: int) -> int:
            active[0] += 1
            most[0] = max(most[0], active[0])
            await asyncio.sleep(0.01)
            active[0] -= 1
            return n * 2

        with worker.Pool(concurrency=3) as pool:
            for n in range(10):
                pool.submit(fetch, n)
            results = pool.join()
        self.assertEqual(sorted(r.value for r in results), [n * 2 for n in range(10)])
        self.assertLessEqual(most[0], 3)

        cancelled = threading.Event()

        async def stuck(_: int) -> None:
            try:
                await asyncio.sleep(LONG)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with worker.Pool(timeout=0.05) as pool:
            pool.submit(stuck, 1)
            results = pool.join()
        self.assertIsInstance(results[0].error, worker.StalledError)
        self.assertTrue(cancelled.wait(LONG))

    def test_timeout_frees_slot_and_preserves_task_timeout_error(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        def run(n: int) -> int:
            if n == 0:
                release.wait(LONG)
            return n

        with worker.Pool(concurrency=1, timeout=0.05) as pool:
            for n in range(3):
                pool.submit(run, n)
            results = pool.join()
        by_item = {r.item: r for r in results}
        self.assertIsInstance(by_item[0].error, worker.StalledError)
        self.assertEqual((by_item[1].value, by_item[2].value), (1, 2))

        def boom(_: int) -> None:
            raise TimeoutError("socket timed out")

        with worker.Pool(timeout=1.0) as pool:
            pool.submit(boom, 1)
            res = pool.join()[0]
        self.assertIs(type(res.error), TimeoutError)
        self.assertEqual(str(res.error), "socket timed out")

    def test_timeout_reuses_worker_threads(self) -> None:
        threads_created = 0
        real_thread = threading.Thread

        def counting_thread(*args, **kwargs):
            nonlocal threads_created
            threads_created += 1
            return real_thread(*args, **kwargs)

        with mock.patch.object(threading, "Thread", side_effect=counting_thread):
            with worker.Pool(concurrency=2, timeout=1.0) as pool:
                for n in range(20):
                    pool.submit(square, n)
                results = pool.join()
        self.assertEqual(len(results), 20)
        self.assertLessEqual(threads_created, 6)


class RetryAndProgressTest(WorkerCase):
    def test_waits_double_with_jitter_up_to_a_minute(self) -> None:
        fake = clock.Fake()
        with clock.use(fake), worker.Pool(retries=9) as pool:
            pool.submit(lambda _: 1 / 0, 0)
            result = pool.join()[0]
        self.assertEqual(result.attempts, 10)
        self.assertIsInstance(result.error, ZeroDivisionError)
        self.assertEqual(len(fake.slept), 9)
        for attempt, wait in enumerate(fake.slept):
            expected = 2 ** attempt
            self.assertGreaterEqual(wait, min(60.0, expected * 0.75))
            self.assertLessEqual(wait, min(60.0, expected * 1.25))
        self.assertEqual(fake.slept[-1], 60.0)

    def test_a_timed_out_try_is_retried(self) -> None:
        tries = []

        def slow_once(n: int) -> int:
            tries.append(n)
            if len(tries) == 1:
                time.sleep(0.5)
            return n

        with worker.Pool(retries=1, timeout=0.05) as pool:
            pool.submit(slow_once, 7)
            result = pool.join()[0]
        self.assertEqual((result.value, result.attempts), (7, 2))

    def test_progress_callback_and_raising_callback(self) -> None:
        calls = []
        with worker.Pool(
            concurrency=1, progress=lambda done, total: calls.append((done, total))
        ) as pool:
            pool.submit(square, 1)
            pool.join()
            pool.submit(square, 2)
        self.assertEqual(calls, [(1, 1), (2, 2)])

        ran = []

        def broken(done: int, total: int) -> None:
            raise RuntimeError("bad callback")

        with self.assertRaisesRegex(RuntimeError, "bad callback"):
            with worker.Pool(concurrency=1, progress=broken) as pool:
                for n in range(6):
                    pool.submit(ran.append, n)
        self.assertLess(len(ran), 6)

    def test_bad_options(self) -> None:
        for options in ({"concurrency": 0}, {"retries": -1}, {"timeout": 0}):
            with self.subTest(options), self.assertRaises(ValueError):
                worker.Pool(**options)  # type: ignore[arg-type]


class CtrlCTest(WorkerCase):
    def test_first_press_cancels_queued_and_pending_retries(self) -> None:
        running, retrying, release = threading.Event(), threading.Event(), threading.Event()
        ran = []

        class WaitToRetry(clock.Clock):
            def sleep(self, seconds: float, *, stop: threading.Event | None = None) -> bool:
                retrying.set()
                return not (stop is not None and stop.wait(LONG))

        def slow() -> None:
            running.set()
            release.wait(LONG)
            ran.append("slow")

        def fail() -> None:
            ran.append("fail")
            raise OSError("down")

        with clock.use(WaitToRetry()), self.assertRaises(KeyboardInterrupt):
            with worker.Pool(concurrency=2, retries=5) as pool:
                pool.submit(slow)
                pool.submit(fail)
                pool.submit(ran.append, "queued")
                self.assertTrue(running.wait(LONG) and retrying.wait(LONG))
                self.after_line("stopping", release.set)
                raise KeyboardInterrupt
        self.assertEqual(sorted(ran), ["fail", "slow"])
        self.assertEqual(self.lines(), [STOPPING])

    def test_second_press_stops_at_once(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        started = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            with worker.Pool(concurrency=1) as pool:
                pool.submit(release.wait, LONG)
                wait_until(lambda: pool._run.running == 1)
                self.after_line("stopping", _thread.interrupt_main)
                raise KeyboardInterrupt
        self.assertLess(time.monotonic() - started, LONG / 2)
        self.assertFalse(release.is_set())
        self.assertEqual(self.lines()[0], STOPPING)


class LoopTest(WorkerCase):
    def test_runs_coroutines_and_survives_task_errors(self) -> None:
        async def fail() -> None:
            raise ValueError("bad")

        with worker.Loop() as loop:
            self.assertIsNone(loop.raw)
            self.assertEqual(loop.run(async_square(4)), 16)
            self.assertIsNotNone(loop.raw)
            with self.assertRaisesRegex(ValueError, "bad"):
                loop.run(fail())
            self.assertEqual(loop.run(async_square(5)), 25)
        self.assertIsNone(loop.raw)

    def test_timeout_raises_stalled_and_closed_raises(self) -> None:
        async def wait_forever() -> None:
            await asyncio.sleep(LONG)

        with worker.Loop() as loop:
            with self.assertRaises(worker.StalledError):
                loop.run(wait_forever(), timeout=0.05)
        with self.assertRaises(worker.ClosedError):
            loop.run(async_square(1))


if __name__ == "__main__":
    unittest.main()
