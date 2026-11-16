import contextvars
import threading
import unittest

import shared
from shared import _clock as clock


class ErrorTest(unittest.TestCase):
    def test_error_inherits_shared_error(self):
        self.assertTrue(issubclass(clock.Error, shared.Error))


class RealClockTest(unittest.TestCase):
    def test_now_wall_and_sleep(self):
        self.assertIsInstance(clock.now(), float)
        self.assertIsInstance(clock.wall(), float)
        self.assertTrue(clock.sleep(0.0))

    def test_sleep_returns_false_when_stop_is_set(self):
        stop = threading.Event()
        stop.set()
        self.assertFalse(clock.sleep(10.0, stop=stop))
        with self.assertRaises(TypeError):
            clock.sleep(0.0, stop)  # pyright: ignore[reportCallIssue]


class FakeClockTest(unittest.TestCase):
    def test_private_time_and_advancement(self):
        fake = clock.Fake(10.0)
        self.assertFalse(hasattr(fake, "time"))
        self.assertEqual(fake.now, 10.0)
        self.assertEqual(fake.wall, clock.FAKE_WALL)

        self.assertTrue(fake.sleep(2.5))
        self.assertEqual(fake.slept, [2.5])
        self.assertEqual(fake.now, 12.5)
        self.assertEqual(fake.wall, clock.FAKE_WALL + 2.5)

        fake.advance(5.0)
        self.assertEqual(fake.now, 17.5)
        self.assertEqual(fake.wall, clock.FAKE_WALL + 7.5)
        self.assertEqual(fake.slept, [2.5])

    def test_sleep_with_stop_and_negative_seconds(self):
        fake = clock.Fake()
        self.assertTrue(fake.sleep(-3.0))
        self.assertEqual(fake.slept, [-3.0])
        self.assertEqual(fake.now, 0.0)

        stop = threading.Event()
        stop.set()
        self.assertFalse(fake.sleep(5.0, stop=stop))
        self.assertEqual(fake.slept, [-3.0])
        self.assertEqual(fake.now, 0.0)

    def test_thread_safe_sleep_and_advance(self):
        fake = clock.Fake()

        def worker():
            for _ in range(50):
                fake.sleep(1.0)
                fake.advance(1.0)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(fake.slept), 200)
        self.assertEqual(fake.now, 400.0)


class UseIsolationTest(unittest.TestCase):
    def test_use_swaps_and_restores_on_exit_and_error(self):
        outer = clock.Fake(start=1.0)
        inner = clock.Fake(start=100.0)
        with clock.use(outer) as bound:
            self.assertIs(bound, outer)
            self.assertEqual(clock.now(), 1.0)
            with self.assertRaises(RuntimeError):
                with clock.use(inner):
                    self.assertEqual(clock.now(), 100.0)
                    clock.sleep(3.0)
                    raise RuntimeError("boom")
            self.assertEqual(clock.now(), 1.0)

    def test_concurrent_threads_and_contexts_never_collide(self):
        results: dict[int, float] = {}
        barrier = threading.Barrier(2)

        def run(idx: int, start: float) -> None:
            with clock.use(clock.Fake(start=start)):
                barrier.wait(timeout=2.0)
                clock.sleep(idx + 1.0)
                results[idx] = clock.now()

        t1 = threading.Thread(target=run, args=(1, 10.0))
        t2 = threading.Thread(target=run, args=(2, 1000.0))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(results, {1: 12.0, 2: 1003.0})

        with clock.use(clock.Fake(start=50.0)):
            ctx = contextvars.copy_context()

            def in_copied_context() -> float:
                with clock.use(clock.Fake(start=999.0)):
                    return clock.now()

            self.assertEqual(ctx.run(in_copied_context), 999.0)
            self.assertEqual(clock.now(), 50.0)


if __name__ == "__main__":
    unittest.main()
