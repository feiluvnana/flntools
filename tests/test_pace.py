"""flnd/pace.py: budgets, rate-limit headers and the learned speed, on a fake clock whose sleep advances it."""

import unittest
from types import SimpleNamespace

from flnd.pace import Pace, Pacer

SITE = "https://www.example.com"
CDN = "https://cdn.example.net/a.jpg"
WALL = 1_800_000_000.0                                   # a Unix time in 2027, for Twitter's reset headers


def response(**headers):
    return SimpleNamespace(headers={name.replace("_", "-"): str(value) for name, value in headers.items()})


class PaceCase(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.waits = []
        self.shown = []

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

    def pacer(self, pace=Pace(), domains=("example.com",)):
        pacer = Pacer(pace, domains, on_wait=self.shown.append, clock=lambda: self.now,
                      wall=lambda: WALL + self.now, sleep=self.sleep)
        pacer._random.seed(3)
        return pacer


class PaceTest(PaceCase):
    def test_a_number_is_a_floor(self):
        self.assertEqual(Pace.of(1.5), Pace(floor=1.5))
        pace = Pace(floor=1.0, budgets={"/graphql": (275, 600)})
        self.assertIs(Pace.of(pace), pace)
        self.assertEqual(self.pacer(2).pace.floor, 2.0)

    def test_the_first_request_goes_at_once_and_the_next_after_the_floor(self):
        pacer = self.pacer(Pace(floor=1.0))
        self.assertEqual(pacer.wait_turn(f"{SITE}/a"), 0.0)
        self.assertGreaterEqual(pacer.wait_turn(f"{SITE}/b"), 1.0)

    def test_jitter_never_goes_below_the_floor(self):
        # Before, "±40%" went down to 0.18 s for a 0.3 s floor; the floor is now the least.
        pacer = self.pacer(Pace(floor=0.5))
        pacer.wait_turn(f"{SITE}/0")
        gaps = [pacer.wait_turn(f"{SITE}/{n}") for n in range(1, 500)]
        self.assertGreaterEqual(min(gaps), 0.5)
        self.assertLessEqual(max(gaps), 0.5 * 1.4)
        self.assertGreater(max(gaps) - min(gaps), 0.1)

    def test_a_url_outside_the_domains_never_waits(self):
        pacer = self.pacer(Pace(floor=5.0, budgets={"/a": (1, 600)}))
        pacer.wait_turn(f"{SITE}/a")
        pacer.slow_down()
        for _ in range(10):
            self.assertEqual(pacer.wait_turn(CDN), 0.0)
            self.assertEqual(pacer.wait_turn("https://notexample.com/a"), 0.0)
        self.assertEqual(self.waits, [])
        self.assertTrue(pacer.paces("https://api.EXAMPLE.com/x"))

    def test_a_long_wait_is_shown(self):
        pacer = self.pacer(Pace(floor=10.0))
        pacer.wait_turn(f"{SITE}/a")
        pacer.wait_turn(f"{SITE}/b")
        self.assertEqual(self.shown, [])
        pacer.slow_down()
        waited = pacer.wait_turn(f"{SITE}/c")
        self.assertEqual(self.shown, [waited])


class BudgetTest(PaceCase):
    def test_a_window_fills_at_the_floors_pace_then_waits_for_its_oldest(self):
        pacer = self.pacer(Pace(floor=0.5, budgets={"/graphql": (275, 600)}))
        first = self.now
        for n in range(275):
            self.assertLessEqual(pacer.wait_turn(f"{SITE}/graphql/{n}"), 0.5 * 1.4)
        # The 276th waits until the first leaves the 600 s window, plus the 5% margin.
        pacer.wait_turn(f"{SITE}/graphql/last")
        self.assertAlmostEqual(self.now, first + 600 * 1.05)

    def test_a_url_without_the_key_is_not_counted(self):
        pacer = self.pacer(Pace(floor=0.5, budgets={"/graphql": (2, 600)}))
        pacer.wait_turn(f"{SITE}/graphql/1")
        for n in range(10):
            self.assertLess(pacer.wait_turn(f"{SITE}/api/{n}"), 1.0)
        pacer.wait_turn(f"{SITE}/graphql/2")
        self.assertLess(self.now, 600)
        pacer.wait_turn(f"{SITE}/graphql/3")
        self.assertAlmostEqual(self.now, 630)


class HeaderTest(PaceCase):
    def test_few_remaining_waits_for_the_reset_plus_a_second(self):
        for prefix in ("x_rate_limit_", "x_ratelimit_"):
            with self.subTest(prefix=prefix):
                self.now = 0.0
                pacer = self.pacer()
                pacer.wait_turn(f"{SITE}/a")
                pacer.record(response(**{prefix + "remaining": 3, prefix + "reset": WALL + 40}))
                self.assertAlmostEqual(pacer.wait_turn(f"{SITE}/b"), 41.0)

    def test_a_reset_in_seconds_from_now(self):
        pacer = self.pacer()
        pacer.record(response(x_ratelimit_remaining=0, x_ratelimit_reset=20))
        self.assertAlmostEqual(pacer.wait_turn(f"{SITE}/a"), 21.0)

    def test_many_remaining_or_no_headers_add_nothing(self):
        pacer = self.pacer()
        pacer.record(response(x_rate_limit_remaining=50, x_rate_limit_reset=WALL + 40))
        pacer.record(response(content_type="text/html"))
        pacer.record(response(x_rate_limit_remaining="many", x_rate_limit_reset="soon"))
        self.assertEqual(pacer.wait_turn(f"{SITE}/a"), 0.0)
        self.assertLess(pacer.wait_turn(f"{SITE}/b"), 1.0)


class SpeedTest(PaceCase):
    def test_a_throttle_doubles_both_floors_up_to_8x(self):
        pacer = self.pacer(Pace(floor=0.5))
        self.assertEqual((pacer.request_floor, pacer.scroll_floor), (0.5, 0.3))
        for speed in (2, 4, 8, 8):
            pacer.slow_down()
            self.assertEqual(pacer.speed, speed)
        self.assertEqual((pacer.request_floor, pacer.scroll_floor), (4.0, 0.3 * 8))
        pacer.wait_turn(f"{SITE}/a")
        self.assertGreaterEqual(pacer.wait_turn(f"{SITE}/b"), 4.0)

    def test_50_clean_requests_bring_it_20_percent_back_toward_1x(self):
        pacer = self.pacer()
        pacer.slow_down()
        pacer.slow_down()
        for _ in range(49):
            pacer.wait_turn(f"{SITE}/a")
        self.assertEqual(pacer.speed, 4)
        pacer.wait_turn(f"{SITE}/a")
        self.assertAlmostEqual(pacer.speed, 1 + 3 * 0.8)
        # Scrolls count as clean too, and it never goes below 1x.
        for _ in range(50 * 200):
            pacer.clean()
        self.assertGreaterEqual(pacer.speed, 1.0)
        self.assertLess(pacer.speed, 1.001)

    def test_a_throttle_restarts_the_clean_count(self):
        pacer = self.pacer()
        pacer.slow_down()
        for _ in range(40):
            pacer.clean()
        pacer.slow_down()
        for _ in range(40):
            pacer.clean()
        self.assertEqual(pacer.speed, 4)

    def test_cdn_downloads_dont_count_as_clean(self):
        pacer = self.pacer()
        pacer.slow_down()
        for _ in range(100):
            pacer.wait_turn(CDN)
        self.assertEqual(pacer.speed, 2)

    def test_the_speed_is_kept_in_state(self):
        state = {}
        pacer = self.pacer()
        pacer.load(state)
        self.assertEqual(pacer.speed, 1.0)
        pacer.slow_down()
        pacer.slow_down()
        pacer.save(state)
        later = self.pacer()
        later.load(state)
        self.assertEqual(later.speed, 4.0)
        self.assertEqual(list(state), ["pace.speed"])
        # A value from a damaged or hand-edited state is kept within 1x..8x, or ignored.
        for saved, speed in ((100, 8.0), (0.1, 1.0), ("fast", 1.0)):
            state["pace.speed"] = saved
            fresh = self.pacer()
            fresh.load(state)
            self.assertEqual(fresh.speed, speed)


if __name__ == "__main__":
    unittest.main()
