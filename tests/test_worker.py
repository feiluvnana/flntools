"""`shared.worker`: `as_ordered`, `as_completed` and `pool`, over threads, processes and coroutines.

Tasks run for real, on real threads and processes; waits end on events, never on sleeps that
"let things happen". The screen is a fake one, so progress rows and warning lines are read back
as text, and Ctrl-C is sent with `_thread.interrupt_main`. Retry waits are replaced, so a test
never waits a minute.
"""

import _thread
import asyncio
import gc
import multiprocessing
import os
import threading
import time
import unittest
from io import StringIO
from unittest import mock

from shared import theme, worker
from shared import _worker
from shared._theme import Screen

# Generous limits for waits that end on an event; tests never sleep to "let things happen".
LONG = 10.0
STOPPING = "! stopping: waiting for 1 running tasks (Ctrl-C again to force)"


def square(n: int) -> int:
    return n * n


def meet(barrier) -> int:
    barrier.wait(LONG)
    return os.getpid()


def sleep_or_pid(seconds: float) -> int:
    time.sleep(seconds)
    return os.getpid()


def wait_until(condition, limit: float = LONG) -> None:
    deadline = time.monotonic() + limit
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.005)


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self.screen = Screen(out=StringIO(), inp=StringIO(""), width=200, tty=False, interactive=False)
        self.scope = theme.scope(screen=self.screen, color="none", unicode=True)
        self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)

    def lines(self) -> list[str]:
        return self.screen.out.getvalue().splitlines()

    def after_line(self, text: str, action) -> None:
        """Run `action` in a thread once `text` is on the screen."""
        def watch():
            wait_until(lambda: text in self.screen.out.getvalue())
            action()

        threading.Thread(target=watch, daemon=True).start()


class AsOrderedTest(WorkerCase):
    def test_input_order(self):
        gates = [threading.Event() for _ in range(4)]

        def last_finishes_first(n):
            if n < 3:
                gates[n + 1].wait(LONG)
            gates[n].set()
            return n * 10

        self.assertEqual(worker.as_ordered(last_finishes_first, range(4), concurrency=4), [0, 10, 20, 30])

    def test_failure_raises_after_the_others_finish(self):
        def maybe(n):
            if n % 3 == 0:
                raise ValueError(f"bad {n}")
            return n

        with self.assertRaises(worker.TaskError) as caught:
            worker.as_ordered(maybe, range(6), concurrency=1)
        error = caught.exception
        self.assertEqual([result.item for result in error.results], list(range(6)))
        self.assertEqual([result.value for result in error.results if result.ok], [1, 2, 4, 5])
        self.assertEqual(str(error), "2 of 6 tasks failed; first: 0: ValueError bad 0")

    def test_several_iterables_are_paired(self):
        self.assertEqual(worker.as_ordered(lambda a, b: a + b, [1, 2, 3], [10, 20]), [11, 22])

    def test_empty_input(self):
        self.assertEqual(worker.as_ordered(square, []), [])

    def test_lazy_input_is_read_a_few_at_a_time(self):
        read = []
        release = threading.Event()

        def numbers():
            for n in range(1000):
                read.append(n)
                yield n

        def hold(n):
            release.wait(LONG)
            return n

        done = []
        runner = threading.Thread(target=lambda: done.append(worker.as_ordered(hold, numbers(), concurrency=2)))
        runner.start()
        wait_until(lambda: len(read) >= 7)
        # 2 running, 4 queued, and the producer blocked holding the 7th.
        self.assertEqual(len(read), 7)
        release.set()
        runner.join(LONG)
        self.assertEqual(done[0], list(range(1000)))


class AsCompletedTest(WorkerCase):
    def test_every_result_in_finish_order_and_never_raises(self):
        gates = {n: threading.Event() for n in range(3)}

        def run(n):
            gates[n].wait(LONG)
            if n == 1:
                raise ValueError("bad")
            return n

        results = worker.as_completed(run, [0, 1, 2], concurrency=3)
        order = []
        for n in (2, 0, 1):
            gates[n].set()
            result = next(results)
            order.append((result.item, result.ok))
        self.assertEqual(order, [(2, True), (0, True), (1, False)])
        self.assertEqual(list(results), [])

    def test_result_fields(self):
        (result,) = list(worker.as_completed(square, [3]))
        self.assertEqual((result.item, result.value, result.error, result.ok, result.attempts), (3, 9, None, True, 1))
        (pair,) = list(worker.as_completed(lambda a, b: a * b, [2], [5]))
        self.assertEqual((pair.item, pair.value), ((2, 5), 10))

    def test_more_items_than_the_queue_holds(self):
        results = list(worker.as_completed(square, range(50), concurrency=2))
        self.assertEqual(sorted(result.value for result in results), [n * n for n in range(50)])

    def test_early_break_stops_quietly(self):
        ran = []
        for _ in worker.as_completed(ran.append, range(100), concurrency=1):
            break
        self.assertLess(len(ran), 100)
        self.assertEqual(self.lines(), [])


class PoolTest(WorkerCase):
    def test_submit_while_tasks_run(self):
        first_started, release = threading.Event(), threading.Event()

        def first():
            first_started.set()
            release.wait(LONG)
            return "first"

        with worker.pool(concurrency=2) as pool:
            pool.submit(first)
            self.assertTrue(first_started.wait(LONG))
            pool.submit(square, 4)
            self.assertEqual(pool.submitted, 2)
            release.set()
        self.assertEqual(sorted(str(result.value) for result in pool.results), ["16", "first"])

    def test_submit_waits_when_the_queue_is_full(self):
        release = threading.Event()
        submitted = []
        with worker.pool(concurrency=1) as pool:
            def produce():
                for n in range(5):
                    pool.submit(release.wait, LONG)
                    submitted.append(n)

            producer = threading.Thread(target=produce)
            producer.start()
            wait_until(lambda: len(submitted) >= 3)
            producer.join(0.1)
            # 1 running and 2 queued: the 4th submit waits.
            self.assertTrue(producer.is_alive())
            self.assertEqual(len(submitted), 3)
            release.set()
            producer.join(LONG)
        self.assertEqual(len(pool.results), 5)

    def test_failures_are_results(self):
        with worker.pool(concurrency=2) as pool:
            pool.submit(square, 2)
            pool.submit(square, "x")
        self.assertEqual(sorted(result.ok for result in pool.results), [False, True])

    def test_close_then_submit_raises_and_close_twice_is_fine(self):
        pool = worker.pool()
        pool.close()
        pool.close()
        with self.assertRaises(RuntimeError):
            pool.submit(square, 1)
        self.assertEqual(pool.join(), [])

    def test_join_before_close_then_submit_more(self):
        pool = worker.pool(concurrency=2)
        pool.submit(square, 1)
        pool.submit(square, 2)
        self.assertEqual(sorted(result.value for result in pool.join()), [1, 4])
        pool.submit(square, 3)
        pool.close()
        self.assertEqual(sorted(result.value for result in pool.join()), [1, 4, 9])

    def test_join_timeout_returns_what_finished(self):
        release = threading.Event()
        pool = worker.pool(concurrency=2)
        pool.submit(square, 5)
        pool.submit(release.wait, LONG)
        wait_until(lambda: len(pool.results) == 1)
        self.assertEqual([result.value for result in pool.join(timeout=0.05)], [25])
        release.set()
        pool.close()
        self.assertEqual(len(pool.join()), 2)
        self.assertEqual(self.lines(), [])

    def test_leaving_the_block_closes_and_joins(self):
        with worker.pool(concurrency=1) as pool:
            for n in range(5):
                pool.submit(square, n)
        self.assertEqual(len(pool.results), 5)
        with self.assertRaises(RuntimeError):
            pool.submit(square, 1)

    def test_warning_when_never_joined(self):
        release = threading.Event()
        self.addCleanup(release.set)
        pool = worker.pool()
        pool.submit(release.wait, LONG)
        del pool
        gc.collect()
        self.assertEqual(self.lines(), ["! worker.pool was never joined; 1 tasks may not have finished"])

    def test_block_error_drops_queued_tasks_with_a_line(self):
        started, release = threading.Event(), threading.Event()
        ran = []

        def slow():
            started.set()
            release.wait(LONG)
            ran.append("slow")

        with self.assertRaises(RuntimeError):
            with worker.pool(concurrency=1) as pool:
                pool.submit(slow)
                pool.submit(ran.append, "queued")
                pool.submit(ran.append, "queued")
                started.wait(LONG)
                release.set()
                raise RuntimeError("body")
        self.assertEqual(ran, ["slow"])
        self.assertEqual(self.lines(), ["! dropped 2 queued tasks after RuntimeError"])


class ProcessesTest(WorkerCase):
    @unittest.skipUnless((os.cpu_count() or 1) >= 2, "needs 2 or more CPUs")
    def test_two_tasks_run_at_the_same_time(self):
        # The barrier opens only when both tasks wait on it, so passing it proves they ran at once.
        with multiprocessing.Manager() as manager:
            barrier = manager.Barrier(2)
            pids = worker.as_ordered(meet, [barrier, barrier], processes=True, concurrency=2)
        self.assertEqual(len(set(pids)), 2)
        self.assertNotIn(os.getpid(), pids)

    def test_lambda_gives_a_clear_type_error(self):
        message = "worker can't send <lambda> to another process: use a function defined at the top level"
        with self.assertRaisesRegex(TypeError, message):
            worker.as_ordered(lambda n: n, [1], processes=True)

    def test_results_and_errors(self):
        self.assertEqual(worker.as_ordered(square, [1, 2, 3], processes=True, concurrency=2), [1, 4, 9])
        with self.assertRaises(worker.TaskError):
            worker.as_ordered(square, ["x"], processes=True, concurrency=1)

    @unittest.skipUnless(hasattr(_worker.signal, "setitimer"), "the try stops itself only where alarms exist")
    def test_timeout_stops_the_try_in_its_process(self):
        started = time.monotonic()
        results = list(worker.as_completed(sleep_or_pid, [LONG, 0], processes=True, concurrency=1, timeout=1.0))
        # One process: the second task could only run once the first try had really stopped.
        self.assertLess(time.monotonic() - started, LONG / 2)
        errors = {result.item: type(result.error) for result in results}
        self.assertEqual(errors, {LONG: TimeoutError, 0: type(None)})


class AsyncTest(WorkerCase):
    def test_at_most_concurrency_at_once(self):
        active, most = [0], [0]

        async def fetch(n):
            active[0] += 1
            most[0] = max(most[0], active[0])
            await asyncio.sleep(0.01)
            active[0] -= 1
            return n * 2

        self.assertEqual(worker.as_ordered(fetch, range(10), concurrency=3), [n * 2 for n in range(10)])
        self.assertLessEqual(most[0], 3)

    def test_timeout_cancels_the_coroutine(self):
        cancelled = threading.Event()

        async def stuck(_):
            try:
                await asyncio.sleep(LONG)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with self.assertRaises(worker.TaskError) as caught:
            worker.as_ordered(stuck, [1], timeout=0.05)
        self.assertIsInstance(caught.exception.results[0].error, TimeoutError)
        self.assertTrue(cancelled.wait(LONG))


class TimeoutTest(WorkerCase):
    def test_thread_runs_on_and_the_slot_is_freed(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def run(n):
            if n == 0:
                release.wait(LONG)
            return n

        with self.assertRaises(worker.TaskError) as caught:
            worker.as_ordered(run, [0, 1, 2], concurrency=1, timeout=0.05)
        results = caught.exception.results
        self.assertIsInstance(results[0].error, TimeoutError)
        self.assertEqual((results[1].value, results[2].value), (1, 2))
        self.assertFalse(release.is_set())


class RetryTest(WorkerCase):
    def setUp(self):
        super().setUp()
        self.waits = []
        patcher = mock.patch.object(_worker, "_sleep", lambda seconds, stop: self.waits.append(seconds))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_waits_double_with_jitter_up_to_a_minute(self):
        (result,) = list(worker.as_completed(lambda _: 1 / 0, [0], retries=9))
        self.assertEqual(result.attempts, 10)
        self.assertIsInstance(result.error, ZeroDivisionError)
        self.assertEqual(len(self.waits), 9)
        for attempt, wait in enumerate(self.waits):
            expected = 2 ** attempt
            self.assertGreaterEqual(wait, min(60.0, expected * 0.75))
            self.assertLessEqual(wait, min(60.0, expected * 1.25))
        self.assertEqual(self.waits[-1], 60.0)

    def test_success_after_a_retry_counts_attempts(self):
        tries = []

        def second_time(n):
            tries.append(n)
            if len(tries) < 2:
                raise OSError("again")
            return n

        (result,) = list(worker.as_completed(second_time, [1], retries=3))
        self.assertEqual((result.ok, result.attempts), (True, 2))

    def test_a_timed_out_try_is_retried(self):
        tries = []

        def slow_once(n):
            tries.append(n)
            if len(tries) == 1:
                time.sleep(0.5)
            return n

        (result,) = list(worker.as_completed(slow_once, [7], retries=1, timeout=0.05))
        self.assertEqual((result.value, result.attempts), (7, 2))


class CtrlCTest(WorkerCase):
    def test_first_press_cancels_queued_and_pending_retries(self):
        running, retrying, release = threading.Event(), threading.Event(), threading.Event()
        ran = []

        def wait_to_retry(seconds, stop_requested):
            retrying.set()
            return stop_requested.wait(LONG)

        def slow():
            running.set()
            release.wait(LONG)
            ran.append("slow")

        def fail():
            ran.append("fail")
            raise OSError("down")

        with mock.patch.object(_worker, "_sleep", wait_to_retry), self.assertRaises(KeyboardInterrupt):
            with worker.pool(concurrency=2, retries=5) as pool:
                pool.submit(slow)
                pool.submit(fail)
                pool.submit(ran.append, "queued")
                self.assertTrue(running.wait(LONG) and retrying.wait(LONG))
                # The running task may finish only after the pool has said it is waiting for it.
                self.after_line("stopping", release.set)
                raise KeyboardInterrupt
        self.assertEqual(sorted(ran), ["fail", "slow"])
        self.assertEqual(sorted((result.ok, result.attempts) for result in pool.results), [(False, 1), (True, 1)])
        self.assertEqual(self.lines(), [STOPPING])

    def test_first_press_while_waiting_at_the_end(self):
        release = threading.Event()
        ran = []

        def interrupt_once_waiting():
            wait_until(lambda: pool._run.closed)
            _thread.interrupt_main()
            release.wait(LONG)
            ran.append("running")

        with self.assertRaises(KeyboardInterrupt):
            with worker.pool(concurrency=1) as pool:
                pool.submit(interrupt_once_waiting)
                pool.submit(ran.append, "queued")
                self.after_line("stopping", release.set)
        self.assertEqual(ran, ["running"])
        self.assertEqual(self.lines(), [STOPPING])

    def test_second_press_stops_at_once(self):
        release = threading.Event()
        self.addCleanup(release.set)
        started = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            with worker.pool(concurrency=1, label="files") as pool:
                pool.submit(release.wait, LONG)
                wait_until(lambda: pool._run.running == 1)
                self.after_line("stopping", _thread.interrupt_main)
                raise KeyboardInterrupt
        self.assertLess(time.monotonic() - started, LONG / 2)
        self.assertFalse(release.is_set())
        self.assertEqual(self.lines()[0], STOPPING)
        self.assertTrue(self.lines()[-1].startswith("✗ files"))

    def test_ctrl_c_inside_a_task_stops_the_run(self):
        ran = []

        def answer(n):
            ran.append(n)
            if n == 0:
                raise KeyboardInterrupt
            return n

        with self.assertRaises(KeyboardInterrupt):
            worker.as_ordered(answer, range(6), concurrency=1)
        self.assertEqual(ran, [0])


class ShutdownTest(WorkerCase):
    def test_a_failing_shutdown_is_logged_and_does_not_hang(self):
        real_stop = _worker._stop_processes

        def stop_then_fail(executor, force):
            real_stop(executor, force)
            raise OSError("pipe closed")

        with mock.patch.object(_worker, "_stop_processes", stop_then_fail):
            self.assertEqual(worker.as_ordered(square, [2], processes=True, concurrency=1), [4])
        self.assertEqual(self.lines(), ["! worker couldn't shut down cleanly: OSError pipe closed"])


class LabelTest(WorkerCase):
    def test_known_total(self):
        worker.as_ordered(square, [1, 2, 3], label="squares")
        self.assertTrue(self.lines()[-1].startswith("✓ squares"))
        self.assertEqual(self.lines()[-1].split()[-1], "3/3")

    def test_growing_total(self):
        with worker.pool(label="files") as pool:
            for n in range(4):
                pool.submit(square, n)
        self.assertEqual(self.lines()[-1].split()[-1], "4/4")
        worker.as_ordered(square, (n for n in range(2)), label="gen")
        self.assertEqual(self.lines()[-1].split()[-1], "2/2")

    def test_failures_end_the_bar_at_error(self):
        with worker.pool(label="bad") as pool:
            pool.submit(square, "x")
        self.assertTrue(self.lines()[-1].startswith("✗ bad"))


class ProgressTest(WorkerCase):
    def test_every_count_once_in_order_before_the_result(self):
        calls = []
        self.assertEqual(worker.as_ordered(square, range(20), concurrency=4,
                                           progress=lambda done, total: calls.append((done, total))),
                         [n * n for n in range(20)])
        # Several runners finish at once, so a count may be skipped, but never repeated or reversed.
        counts = [done for done, _ in calls]
        self.assertEqual(counts, sorted(set(counts)))
        self.assertEqual(calls[-1], (20, 20))
        self.assertEqual({total for _, total in calls}, {20})
        self.assertEqual(self.lines(), [])

    def test_total_is_what_was_read_so_far_without_a_length(self):
        calls = []
        with worker.pool(concurrency=1, progress=lambda done, total: calls.append((done, total))) as pool:
            pool.submit(square, 1)
            pool.join()
            pool.submit(square, 2)
        self.assertEqual(calls, [(1, 1), (2, 2)])

    def test_failed_tasks_count_too(self):
        calls = []
        results = list(worker.as_completed(lambda n: 1 / n, [0, 1], concurrency=1,
                                           progress=lambda done, total: calls.append((done, total))))
        self.assertEqual([result.ok for result in results], [False, True])
        self.assertEqual(calls, [(1, 2), (2, 2)])

    def test_a_raising_callback_stops_the_run_and_reaches_the_caller(self):
        """A bug in the callback must not be lost on a runner thread, nor hang the run."""
        ran = []

        def broken(done, total):
            raise RuntimeError("bad callback")

        with self.assertRaisesRegex(RuntimeError, "bad callback"):
            worker.as_ordered(ran.append, range(6), concurrency=1, progress=broken)
        self.assertLess(len(ran), 6)

    def test_with_a_label_both_report(self):
        calls = []
        worker.as_ordered(square, [1, 2], label="both", progress=lambda done, total: calls.append(done))
        self.assertEqual(calls[-1], 2)
        self.assertTrue(self.lines()[-1].startswith("✓ both"))


class OptionTest(unittest.TestCase):
    def test_bad_options(self):
        for options in ({"concurrency": 0}, {"retries": -1}, {"timeout": 0}):
            ran = []
            with self.subTest(options), self.assertRaises(ValueError):
                worker.as_ordered(ran.append, [1], **options)
            self.assertEqual(ran, [])

    def test_a_progress_label_points_to_label(self):
        with self.assertRaisesRegex(TypeError, "label="):
            worker.as_ordered(square, [1], progress="files")  # pyright: ignore[reportArgumentType]

    def test_removed_options(self):
        for name in ("workers", "rate", "setup", "errors"):
            with self.subTest(name), self.assertRaises(TypeError):
                worker.as_ordered(square, [1], **{name: 1})


if __name__ == "__main__":
    unittest.main()
