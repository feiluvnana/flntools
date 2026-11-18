import contextvars
import datetime
import threading
import unittest

from flnkit import clock as _time


class RealClockTest(unittest.TestCase):
    def test_now_wall_and_sleep(self):
        self.assertIsInstance(_time.now(), float)
        self.assertIsInstance(_time.wall(), float)
        self.assertTrue(_time.sleep(0.0))

    def test_sleep_returns_false_when_stop_is_set(self):
        stop = threading.Event()
        stop.set()
        self.assertFalse(_time.sleep(10.0, stop=stop))
        with self.assertRaises(TypeError):
            _time.sleep(0.0, stop)


class FakeClockTest(unittest.TestCase):
    def test_private_time_and_advancement(self):
        fake = _time.Fake(10.0)
        self.assertFalse(hasattr(fake, "time"))
        self.assertEqual(fake.now, 10.0)
        self.assertEqual(fake.wall, _time.FAKE_WALL)

        self.assertTrue(fake.sleep(2.5))
        self.assertEqual(fake.slept, [2.5])
        self.assertEqual(fake.now, 12.5)
        self.assertEqual(fake.wall, _time.FAKE_WALL + 2.5)

        fake.advance(5.0)
        self.assertEqual(fake.now, 17.5)
        self.assertEqual(fake.wall, _time.FAKE_WALL + 7.5)
        self.assertEqual(fake.slept, [2.5])

    def test_sleep_with_stop_and_negative_seconds(self):
        fake = _time.Fake()
        self.assertTrue(fake.sleep(-3.0))
        self.assertEqual(fake.slept, [-3.0])
        self.assertEqual(fake.now, 0.0)

        stop = threading.Event()
        stop.set()
        self.assertFalse(fake.sleep(5.0, stop=stop))
        self.assertEqual(fake.slept, [-3.0])
        self.assertEqual(fake.now, 0.0)

    def test_thread_safe_sleep_and_advance(self):
        fake = _time.Fake()

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
        outer = _time.Fake(start=1.0)
        inner = _time.Fake(start=100.0)
        with _time.use(outer) as bound:
            self.assertIs(bound, outer)
            self.assertEqual(_time.now(), 1.0)
            with self.assertRaises(RuntimeError):
                with _time.use(inner):
                    self.assertEqual(_time.now(), 100.0)
                    _time.sleep(3.0)
                    raise RuntimeError("boom")
            self.assertEqual(_time.now(), 1.0)

    def test_concurrent_threads_and_contexts_never_collide(self):
        results: dict[int, float] = {}
        barrier = threading.Barrier(2)

        def run(idx: int, start: float) -> None:
            with _time.use(_time.Fake(start=start)):
                barrier.wait(timeout=2.0)
                _time.sleep(idx + 1.0)
                results[idx] = _time.now()

        t1 = threading.Thread(target=run, args=(1, 10.0))
        t2 = threading.Thread(target=run, args=(2, 1000.0))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(results, {1: 12.0, 2: 1003.0})

        with _time.use(_time.Fake(start=50.0)):
            ctx = contextvars.copy_context()

            def in_copied_context() -> float:
                with _time.use(_time.Fake(start=999.0)):
                    return _time.now()

            self.assertEqual(ctx.run(in_copied_context), 999.0)
            self.assertEqual(_time.now(), 50.0)


class DayTest(unittest.TestCase):
    def test_day_reads_today_from_the_fake_clock(self):
        # Far past any real today, so the old `date.today()` bound refuses it.
        with _time.use(_time.Fake()) as clock:
            clock.advance(datetime.datetime(2150, 1, 1, tzinfo=datetime.UTC).timestamp()
                          - _time.FAKE_WALL)
            self.assertEqual(_time.day(_time.wall()), datetime.date(2150, 1, 1))
            self.assertEqual(_time.day("2151-06-01"), datetime.date(2151, 6, 1))
            self.assertIsNone(_time.day("2152-01-01"))

if __name__ == "__main__":
    unittest.main()
