import _thread
import multiprocessing
import os
import threading
import time
import unittest
from io import StringIO
from unittest import mock

from shared import log, theme, worker
from shared import _worker
from shared._theme import Screen

# Generous limits for waits that end on an event; tests never sleep to "let things happen".
LONG = 10.0


def square(n: int) -> int:
    return n * n


def meet_then_burn(barrier) -> int:
    barrier.wait(LONG)
    total = 0
    for i in range(1_000_000):
        total += i * i
    return os.getpid()


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self.screen = Screen(out=StringIO(), inp=StringIO(""), width=200, tty=False, interactive=False)
        self.scope = theme.scope(screen=self.screen, color="none", unicode=True)
        self.scope.__enter__()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def lines(self) -> list[str]:
        return self.screen.out.getvalue().splitlines()


class MapTest(WorkerCase):
    def test_input_order(self):
        def slow_first(n):
            if n == 0:
                time.sleep(0.05)
            return n * 10

        self.assertEqual(worker.map(slow_first, range(8), workers=4), [n * 10 for n in range(8)])

    def test_several_iterables_are_zipped(self):
        self.assertEqual(worker.map(lambda a, b: a + b, [1, 2, 3], [10, 20]), [11, 22])

    def test_empty_input(self):
        self.assertEqual(worker.map(square, []), [])

    def test_lazy_input_is_never_fully_loaded(self):
        read = []
        release = threading.Event()

        def numbers():
            for n in range(1000):
                read.append(n)
                yield n

        def wait(n):
            release.wait(LONG)
            return n

        done = []
        runner = threading.Thread(target=lambda: done.append(worker.map(wait, numbers(), workers=2)))
        runner.start()
        deadline = time.monotonic() + LONG
        while len(read) < 6 and time.monotonic() < deadline:
            time.sleep(0.005)
        # 2 running, 4 queued, and the producer blocked holding the 7th.
        self.assertLessEqual(len(read), 7)
        release.set()
        runner.join(LONG)
        self.assertEqual(done[0], list(range(1000)))

    def test_task_error_keeps_partial_results(self):
        def maybe(n):
            if n % 3 == 0:
                raise ValueError(f"bad {n}")
            return n

        with self.assertRaises(worker.TaskError) as caught:
            worker.map(maybe, range(6), workers=1)
        error = caught.exception
        self.assertEqual(len(error.results), 6)
        self.assertEqual(sorted(result.item for result in error.failed), [0, 3])
        self.assertEqual(sorted(r.value for r in error.results if r.ok), [1, 2, 4, 5])
        self.assertEqual(str(error), "2 of 6 tasks failed; first: 0: ValueError bad 0")


class EachTest(WorkerCase):
    def test_finish_order_and_never_raises(self):
        gates = {n: threading.Event() for n in range(3)}

        def run(n):
            gates[n].wait(LONG)
            if n == 1:
                raise ValueError("bad")
            return n

        results = worker.each(run, [0, 1, 2], workers=3)
        order = []
        for n in (2, 0, 1):
            gates[n].set()
            result = next(results)
            order.append((result.item, result.ok))
        self.assertEqual(order, [(2, True), (0, True), (1, False)])
        self.assertEqual(list(results), [])

    def test_result_fields(self):
        (result,) = list(worker.each(square, [3]))
        self.assertEqual((result.item, result.value, result.error, result.ok, result.attempts), (3, 9, None, True, 1))
        self.assertGreaterEqual(result.seconds, 0)
        (pair,) = list(worker.each(lambda a, b: a * b, [2], [5]))
        self.assertEqual((pair.item, pair.value), ((2, 5), 10))

    def test_early_break_stops_the_pool(self):
        ran = []
        for result in worker.each(ran.append, range(100), workers=1):
            break
        self.assertLess(len(ran), 100)


class PoolTest(WorkerCase):
    def test_submit_while_tasks_run(self):
        first_started, release = threading.Event(), threading.Event()

        def first():
            first_started.set()
            release.wait(LONG)
            return "first"

        with worker.pool(workers=2) as pool:
            pool.submit(first)
            self.assertTrue(first_started.wait(LONG))
            pool.submit(square, 4)
            self.assertEqual(pool.submitted, 2)
            release.set()
        self.assertEqual(sorted(str(r.value) for r in pool.results), ["16", "first"])
        self.assertEqual((pool.succeeded, pool.failed, pool.running), (2, 0, 0))

    def test_submit_blocks_when_queue_is_full(self):
        release = threading.Event()
        submitted = []
        with worker.pool(workers=1) as pool:
            def produce():
                for n in range(5):
                    pool.submit(release.wait, LONG)
                    submitted.append(n)

            producer = threading.Thread(target=produce)
            producer.start()
            deadline = time.monotonic() + LONG
            while len(submitted) < 3 and time.monotonic() < deadline:
                time.sleep(0.005)
            producer.join(0.1)
            # 1 running and 2 queued: the 4th submit is blocked.
            self.assertTrue(producer.is_alive())
            self.assertEqual(len(submitted), 3)
            release.set()
            producer.join(LONG)
        self.assertEqual(pool.succeeded, 5)

    def test_raises_task_error_on_exit(self):
        with self.assertRaises(worker.TaskError):
            with worker.pool(workers=2) as pool:
                pool.submit(square, 2)
                pool.submit(square, "x")
        self.assertEqual((pool.succeeded, pool.failed), (1, 1))

    def test_body_exception_drops_queued_tasks(self):
        started, release = threading.Event(), threading.Event()
        ran = []

        def slow():
            started.set()
            release.wait(LONG)
            ran.append("slow")

        with self.assertRaises(RuntimeError):
            with worker.pool(workers=1) as pool:
                pool.submit(slow)
                pool.submit(ran.append, "queued")
                started.wait(LONG)
                release.set()
                raise RuntimeError("body")
        self.assertEqual(ran, ["slow"])

    def test_submit_after_exit(self):
        with worker.pool() as pool:
            pass
        with self.assertRaises(RuntimeError):
            pool.submit(square, 1)


class DecoratorTest(WorkerCase):
    def test_plain_call_map_each_and_options(self):
        calls = []

        @worker.task(workers=2, retries=1, backoff=0.0)
        def flaky(n):
            calls.append(n)
            if n < 0 or (n == 2 and calls.count(n) == 1):
                raise ValueError("bad")
            return n + 1

        self.assertEqual(flaky(5), 6)
        self.assertEqual(flaky.__name__, "flaky")
        self.assertEqual(flaky.map([1, 2, 3]), [2, 3, 4])
        self.assertEqual(sorted(r.value for r in flaky.each([7, 8])), [8, 9])
        with self.assertRaises(worker.TaskError):
            flaky.map([-1], retries=0, workers=1)
        self.assertEqual(calls.count(-1), 1)
        with self.assertRaises(ValueError):
            worker.task(workers=0)


class RetryTest(WorkerCase):
    def setUp(self):
        super().setUp()
        self.waits = []
        patcher = mock.patch.object(_worker, "_sleep", self.waits.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_retryable_types(self):
        attempts = []

        def fail(kind):
            attempts.append(kind)
            raise kind("x")

        results = list(worker.each(fail, [KeyError, TypeError], retries=2, retryable=(KeyError,), workers=1))
        by_item = {result.item: result.attempts for result in results}
        self.assertEqual(by_item, {KeyError: 3, TypeError: 1})

    def test_backoff_waits(self):
        (result,) = list(worker.each(lambda _: 1 / 0, [0], retries=9, backoff=1.0))
        self.assertEqual(result.attempts, 10)
        self.assertIsInstance(result.error, ZeroDivisionError)
        self.assertEqual(len(self.waits), 9)
        for attempt, wait in enumerate(self.waits):
            expected = 2 ** attempt
            self.assertGreaterEqual(wait, min(60.0, expected * 0.75))
            self.assertLessEqual(wait, min(60.0, expected * 1.25))
        self.assertEqual(self.waits[-1], 60.0)

    def test_success_after_retry_counts_attempts(self):
        tries = []

        def second_time(n):
            tries.append(n)
            if len(tries) < 2:
                raise OSError("again")
            return n

        (result,) = list(worker.each(second_time, [1], retries=3))
        self.assertEqual((result.ok, result.attempts), (True, 2))

    def test_keyboard_interrupt_is_never_retried(self):
        tries = []

        def interrupt(_):
            tries.append(1)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            worker.map(interrupt, [1, 2, 3], retries=3, retryable=(BaseException,), workers=1)
        self.assertEqual(tries, [1])


class RateTest(WorkerCase):
    def test_starts_per_second_stay_within_the_limit(self):
        # The clock never moves, so each wait is exactly how far ahead the start was scheduled.
        waits = []
        with mock.patch.object(_worker, "_clock", lambda: 100.0), mock.patch.object(_worker, "_sleep", waits.append):
            worker.map(square, range(20), workers=4, rate=5)
        self.assertEqual(sorted(round(wait, 6) for wait in waits), [round(n / 5, 6) for n in range(1, 20)])
        starts = [100.0] + [100.0 + wait for wait in waits]
        for start in starts:
            self.assertLessEqual(sum(start <= other < start + 1 for other in starts), 5)


class TimeoutTest(WorkerCase):
    def test_timeout_fails_the_task_and_replaces_the_slot(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def run(n):
            if n == 0:
                release.wait(LONG)
            return n

        with self.assertRaises(worker.TaskError) as caught:
            worker.map(run, [0, 1, 2], workers=1, timeout=0.3)
        results = {result.item: result for result in caught.exception.results}
        self.assertIsInstance(results[0].error, TimeoutError)
        self.assertEqual((results[1].value, results[2].value), (1, 2))
        # The stuck thread is still running in the background.
        self.assertFalse(release.is_set())


class ErrorsLogTest(WorkerCase):
    def test_a_line_per_failure_and_no_raise(self):
        def check(n):
            if n == 0:
                raise ValueError("zero")
            return 10 // n

        values = worker.map(check, [1, 0, 2], workers=1, errors="log")
        self.assertEqual(values, [10, None, 5])
        self.assertEqual(self.lines(), ["✗ 0: ValueError zero"])


class ProgressTest(WorkerCase):
    def test_known_total(self):
        worker.map(square, [1, 2, 3], progress="squares")
        self.assertEqual(self.lines()[-1].split()[-1], "3/3")
        self.assertTrue(self.lines()[-1].startswith("✓ squares"))

    def test_growing_total(self):
        with worker.pool(progress="files") as pool:
            for n in range(4):
                pool.submit(square, n)
        self.assertEqual(self.lines()[-1].split()[-1], "4/4")
        worker.map(square, (n for n in range(2)), progress="gen")
        self.assertEqual(self.lines()[-1].split()[-1], "2/2")

    def test_failures_end_the_bar_at_error(self):
        worker.map(lambda n: 1 / n, [0], progress="bad", errors="log")
        self.assertTrue(self.lines()[-1].startswith("✗ bad"))


class Resource:
    def __init__(self, log_list):
        self.log = log_list

    def __enter__(self):
        self.log.append(("enter", threading.get_ident()))
        return self

    def __exit__(self, *exc_info):
        self.log.append(("exit", threading.get_ident()))


class SetupTest(WorkerCase):
    def test_once_per_worker_and_exited_in_the_same_thread(self):
        events = []
        seen = []

        def use(resource, n):
            seen.append((id(resource), threading.get_ident()))
            time.sleep(0.01)
            return n

        worker.map(use, range(12), workers=3, setup=lambda: Resource(events))
        enters = [ident for kind, ident in events if kind == "enter"]
        exits = [ident for kind, ident in events if kind == "exit"]
        self.assertEqual(len(enters), len(set(enters)))
        self.assertLessEqual(len(enters), 3)
        self.assertEqual(sorted(enters), sorted(exits))
        # Each thread always got its own resource.
        self.assertEqual(len({ident for _, ident in seen}), len({rid for rid, _ in seen}))

    def test_plain_value(self):
        self.assertEqual(worker.map(lambda prefix, n: prefix + str(n), [1, 2], setup=lambda: "n"), ["n1", "n2"])

    def test_exited_on_error_and_on_ctrl_c(self):
        for raised in (RuntimeError, KeyboardInterrupt):
            events = []
            with self.assertRaises(raised):
                with worker.pool(workers=2, setup=lambda: Resource(events)) as pool:
                    pool.submit(lambda resource: None)
                    while pool.succeeded < 1:
                        time.sleep(0.005)
                    raise raised()
            self.assertEqual([kind for kind, _ in events], ["enter", "exit"])

    def test_failing_setup_is_reported_once(self):
        calls = []

        def broken():
            calls.append(1)
            raise OSError("no browser")

        values = worker.map(lambda resource, n: n, range(10), workers=2, setup=broken, errors="log")
        self.assertEqual(values, [None] * 10)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.lines(), ["✗ setup failed: OSError no browser"])

    def test_one_failing_setup_leaves_other_workers(self):
        calls = []
        lock = threading.Lock()

        def first_fails():
            with lock:
                calls.append(1)
                if len(calls) == 1:
                    raise OSError("first")
            return "ok"

        values = worker.map(lambda resource, n: (resource, n), range(6), workers=2, setup=first_fails)
        self.assertEqual(values, [("ok", n) for n in range(6)])


class CtrlCTest(WorkerCase):
    def release_after_warning(self, release):
        deadline = time.monotonic() + LONG
        while "stopping" not in self.screen.out.getvalue() and time.monotonic() < deadline:
            time.sleep(0.005)
        release.set()

    def test_queued_dropped_running_finish(self):
        started, release = threading.Event(), threading.Event()
        ran = []

        def slow():
            started.set()
            release.wait(LONG)
            ran.append("slow")

        with self.assertRaises(KeyboardInterrupt):
            with worker.pool(workers=1) as pool:
                pool.submit(slow)
                pool.submit(ran.append, "queued")
                started.wait(LONG)
                # The running task may finish only after the pool has said it is waiting for it.
                threading.Thread(target=self.release_after_warning, args=(release,)).start()
                raise KeyboardInterrupt
        self.assertEqual(ran, ["slow"])
        self.assertEqual(self.lines(), ["! stopping: waiting for 1 running tasks (Ctrl-C again to force)"])

    def test_ctrl_c_while_waiting_at_the_end(self):
        release = threading.Event()
        ran = []

        def interrupt_once_waiting():
            # pool._closed turns true when the caller has left the block and started waiting.
            while not pool._closed:
                time.sleep(0.005)
            _thread.interrupt_main()
            release.wait(LONG)
            ran.append("running")

        with self.assertRaises(KeyboardInterrupt):
            with worker.pool(workers=1) as pool:
                pool.submit(interrupt_once_waiting)
                pool.submit(ran.append, "queued")
                threading.Thread(target=self.release_after_warning, args=(release,)).start()
        self.assertEqual(ran, ["running"])
        self.assertEqual(self.lines(), ["! stopping: waiting for 1 running tasks (Ctrl-C again to force)"])


class ProcessesTest(WorkerCase):
    @unittest.skipUnless((os.cpu_count() or 1) >= 2, "needs 2 or more CPUs")
    def test_cpu_bound_tasks_run_at_the_same_time(self):
        # Both tasks must reach the barrier before either can go on, so passing it proves they
        # ran at once in two processes. Timing a speed-up instead fails on a busy machine.
        with multiprocessing.Manager() as manager:
            barrier = manager.Barrier(2)
            pids = worker.map(meet_then_burn, [barrier, barrier], processes=True, workers=2)
        self.assertEqual(len(set(pids)), 2)
        self.assertNotIn(os.getpid(), pids)

    def test_lambda_gives_a_clear_type_error(self):
        with self.assertRaisesRegex(TypeError, "module-level function"):
            worker.map(lambda n: n, [1], processes=True)

    def test_results_and_errors(self):
        self.assertEqual(worker.map(square, [1, 2, 3], processes=True, workers=2), [1, 4, 9])
        with self.assertRaises(worker.TaskError):
            worker.map(square, ["x"], processes=True, workers=1)


class OptionTest(unittest.TestCase):
    def test_bad_options(self):
        bad = [
            {"workers": 0}, {"retries": -1}, {"rate": 0}, {"timeout": 0}, {"errors": "ignore"},
            {"setup": lambda: None, "processes": True},
        ]
        for options in bad:
            ran = []
            with self.subTest(options), self.assertRaises(ValueError):
                worker.map(ran.append, [1], **options)
            self.assertEqual(ran, [])


if __name__ == "__main__":
    unittest.main()
