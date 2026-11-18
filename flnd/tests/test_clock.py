"""`flnkit.clock`: the real clock, `Fake`, `use` and `day`."""

import contextvars
import datetime
import threading
import unittest

from flnkit import clock as _time


def run_threads(target, *args_list):
    threads = [threading.Thread(target=target, args=args) for args in args_list]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


class ClockTest(unittest.TestCase):
    def test_real_clock_and_a_set_stop(self):
        self.assertIsInstance(_time.now(), float)
        self.assertIsInstance(_time.wall(), float)
        self.assertTrue(_time.sleep(0.0))
        stop = threading.Event()
        stop.set()
        self.assertFalse(_time.sleep(10.0, stop=stop))
        with self.assertRaises(TypeError):
            _time.sleep(0.0, stop)

    def test_fake_advances_and_records_its_sleeps(self):
        fake = _time.Fake(10.0)
        self.assertFalse(hasattr(fake, "time"))
        self.assertEqual((fake.now, fake.wall), (10.0, _time.FAKE_WALL))
        self.assertTrue(fake.sleep(2.5))
        self.assertEqual((fake.slept, fake.now, fake.wall), ([2.5], 12.5, _time.FAKE_WALL + 2.5))
        fake.advance(5.0)
        self.assertEqual((fake.slept, fake.now, fake.wall), ([2.5], 17.5, _time.FAKE_WALL + 7.5))
        # A negative sleep is recorded but passes no time; a set stop records nothing.
        self.assertTrue(fake.sleep(-3.0))
        stop = threading.Event()
        stop.set()
        self.assertFalse(fake.sleep(5.0, stop=stop))
        self.assertEqual((fake.slept, fake.now), ([2.5, -3.0], 17.5))

    def test_fake_is_thread_safe(self):
        fake = _time.Fake()

        def worker():
            for _ in range(50):
                fake.sleep(1.0)
                fake.advance(1.0)

        run_threads(worker, *[()] * 4)
        self.assertEqual((len(fake.slept), fake.now), (200, 400.0))

    def test_use_swaps_and_restores_on_exit_and_error(self):
        outer, inner = _time.Fake(start=1.0), _time.Fake(start=100.0)
        with _time.use(outer) as bound:
            self.assertIs(bound, outer)
            with self.assertRaises(RuntimeError), _time.use(inner):
                self.assertEqual(_time.now(), 100.0)
                _time.sleep(3.0)
                raise RuntimeError("boom")
            self.assertEqual(_time.now(), 1.0)

    def test_threads_and_contexts_each_keep_their_clock(self):
        results: dict[int, float] = {}
        barrier = threading.Barrier(2)

        def run(idx: int, start: float) -> None:
            with _time.use(_time.Fake(start=start)):
                barrier.wait(timeout=2.0)
                _time.sleep(idx + 1.0)
                results[idx] = _time.now()

        run_threads(run, (1, 10.0), (2, 1000.0))
        self.assertEqual(results, {1: 12.0, 2: 1003.0})

        def in_copied_context() -> float:
            with _time.use(_time.Fake(start=999.0)):
                return _time.now()

        with _time.use(_time.Fake(start=50.0)):
            self.assertEqual(contextvars.copy_context().run(in_copied_context), 999.0)
            self.assertEqual(_time.now(), 50.0)

    def test_day_reads_today_from_the_fake_clock(self):
        # Far past any real today, so a `date.today()` bound would refuse it.
        with _time.use(_time.Fake()) as clock:
            clock.advance(datetime.datetime(2150, 1, 1, tzinfo=datetime.UTC).timestamp()
                          - _time.FAKE_WALL)
            self.assertEqual(_time.day(_time.wall()), datetime.date(2150, 1, 1))
            self.assertEqual(_time.day("2151-06-01"), datetime.date(2151, 6, 1))
            self.assertIsNone(_time.day("2152-01-01"))


if __name__ == "__main__":
    unittest.main()
