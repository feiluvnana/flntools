"""`http.Client` and `http.Pace`: each host's floor, budgets, rate headers and 429 back-off.

The network is `httpx.MockTransport` and the clock is `clock.Fake`, so minutes of back-off take no
time and `fake.slept` is every wait the client made.
"""

import io
import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path

import httpx

from shared import _cookies as cookies
from shared import clock, http, theme
from shared._theme import Screen

SITE = "https://www.example.com"
CDN = "https://cdn.example.net"
PACE = http.Pace(1.0, {"/graphql": (2, 10.0)}, domains=("example.com",))


class ClientCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = self.enterContext(clock.use(clock.Fake()))
        self.screen = Screen(dest=io.StringIO(), inp=io.StringIO(""), width=80, tty=False,
                             interactive=False)
        self.enterContext(theme.scope(screen=self.screen, color="none", unicode=True))
        self.answers: list[httpx.Response] = []
        self.sent: list[tuple[float, str]] = []

    def serve(self, request: httpx.Request) -> httpx.Response:
        self.sent.append((self.fake.now(), str(request.url)))
        return self.answers.pop(0) if self.answers else httpx.Response(200, content=b"ok")

    def client(self, **options) -> http.Client:
        raw = httpx.Client(transport=httpx.MockTransport(self.serve))
        self.addCleanup(raw.close)
        return http.Client(raw, **options)

    def gaps(self) -> list[float]:
        return [later - earlier for (earlier, _), (later, _) in zip(self.sent, self.sent[1:])]


class PaceTest(ClientCase):
    def test_the_floor_spaces_a_named_hosts_requests_with_jitter_that_only_adds(self):
        client = self.client(pace=PACE)
        for _ in range(4):
            client.fetch(f"{SITE}/items")
        for gap in self.gaps():
            self.assertTrue(1.0 <= gap <= 1.4, gap)

    def test_a_host_the_pace_doesnt_name_goes_at_once(self):
        client = self.client(pace=PACE)
        for _ in range(3):
            client.fetch(f"{CDN}/a.jpg")
        self.assertEqual(self.fake.slept, [])

    def test_a_full_budget_waits_for_its_oldest_request_to_leave_the_window(self):
        client = self.client(pace=http.Pace(0.0, {"/graphql": (2, 10.0)}))
        for _ in range(3):
            client.fetch(f"{SITE}/graphql/query")
        self.assertAlmostEqual(self.gaps()[1], 10.5)
        client.fetch(f"{SITE}/other")
        self.assertAlmostEqual(self.gaps()[2], 0.0)

    def test_rate_headers_near_zero_hold_the_host_until_the_reset(self):
        headers = {"x-rate-limit-remaining": "2", "x-rate-limit-reset": "20"}
        self.answers = [httpx.Response(200, headers=headers)]
        client = self.client()
        client.fetch(f"{SITE}/a")
        client.fetch(f"{SITE}/b")
        self.assertAlmostEqual(self.gaps()[0], 21.0)

    def test_a_unix_reset_counts_from_the_wall_clock(self):
        reset = str(int(clock.wall() + 30))
        headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}
        self.answers = [httpx.Response(200, headers=headers)]
        client = self.client()
        client.fetch(f"{SITE}/a")
        client.fetch(f"{SITE}/b")
        self.assertAlmostEqual(self.gaps()[0], 31.0)

    def test_a_labelled_pace_gives_its_hosts_one_pace_named_by_the_label(self):
        client = self.client(pace=http.Pace(1.0, domains=("example.com",), label="exm"),
                             speeds={"exm": 2.0})
        client.fetch(f"{SITE}/items")
        client.fetch("https://api.example.com/items")
        # One floor for both hosts, at the speed the label saved: 2 s plus up to 40%.
        self.assertTrue(2.0 <= self.gaps()[0] <= 2.8, self.gaps())
        client.fetch(f"{CDN}/a.jpg")
        self.assertEqual(client.speeds, {"exm": 2.0, "cdn.example.net": 1.0})

    def test_a_speed_given_but_not_used_is_kept(self):
        self.assertEqual(self.client(speeds={"www.example.com": 4.0}).speeds,
                         {"www.example.com": 4.0})


class BackOffTest(ClientCase):
    def test_a_429_is_waited_out_for_its_retry_after_then_sent_again(self):
        self.answers = [httpx.Response(429, headers={"Retry-After": "7"})]
        client = self.client(pace=PACE)
        page = client.fetch(f"{SITE}/items")
        self.assertEqual((page.status, len(self.sent)), (200, 2))
        self.assertAlmostEqual(self.gaps()[0], 7.0)
        self.assertEqual(client.speeds, {"www.example.com": 2.0})
        self.assertIn("! www.example.com: rate limited; waiting 7.0s (attempt 1)",
                      self.screen.dest.getvalue())

    def test_without_retry_after_the_waits_double_from_30_seconds(self):
        self.answers = [httpx.Response(429), httpx.Response(429)]
        client = self.client()
        client.fetch(f"{SITE}/items")
        first, second = self.gaps()
        self.assertTrue(24 <= first <= 36, first)
        self.assertTrue(48 <= second <= 72, second)

    def test_a_429_with_retry_after_zero_waits(self):
        self.answers = [httpx.Response(429, headers={"Retry-After": "0"})]
        client = self.client()
        page = client.fetch(f"{SITE}/items")
        self.assertEqual(page.status, 200)
        self.assertTrue(24 <= self.gaps()[0] <= 36, self.gaps())

    def test_retry_after_infinite_or_huge_is_clamped(self):
        self.answers = [httpx.Response(429, headers={"Retry-After": "1e400"})]
        client = self.client()
        page = client.fetch(f"{SITE}/items")
        self.assertEqual(page.status, 200)
        self.assertTrue(24 <= self.gaps()[0] <= 36, self.gaps())

    def test_a_cdns_429_never_slows_the_site(self):
        self.answers = [httpx.Response(429, headers={"Retry-After": "60"})]
        client = self.client(pace=PACE)
        client.fetch(f"{CDN}/a.jpg")
        client.fetch(f"{SITE}/items")
        self.assertEqual(client.speeds["www.example.com"], 1.0)
        self.assertEqual(client.speeds["cdn.example.net"], 2.0)

    def test_fifty_clean_requests_ease_the_speed_back(self):
        client = self.client(speeds={"www.example.com": 8.0})
        for _ in range(50):
            client.fetch(f"{SITE}/items")
        self.assertAlmostEqual(client.speeds["www.example.com"], 1 + 7 * 0.8)

    def test_a_stop_ends_a_wait(self):
        stop = threading.Event()
        stop.set()
        self.answers = [httpx.Response(429, headers={"Retry-After": "60"})]
        with self.assertRaises(http.StoppedError):
            self.client(stop=stop).fetch(f"{SITE}/items")

    def test_a_refusal_is_raised_and_its_rate_headers_read(self):
        headers = {"x-rate-limit-remaining": "0", "x-rate-limit-reset": "5"}
        self.answers = [httpx.Response(403, headers=headers)]
        client = self.client()
        with self.assertRaises(http.RefusedError):
            client.fetch(f"{SITE}/a")
        client.fetch(f"{SITE}/b")
        self.assertAlmostEqual(self.gaps()[0], 6.0)

    def test_a_429_without_retry_after_waits_for_its_rate_reset(self):
        headers = {"x-rate-limit-remaining": "0", "x-rate-limit-reset": "40"}
        self.answers = [httpx.Response(429, headers=headers)]
        client = self.client()
        client.fetch(f"{SITE}/items")
        self.assertAlmostEqual(self.gaps()[0], 41.0)

    def test_back_off_counts_a_throttle_seen_elsewhere_and_waits_it_out_once(self):
        client = self.client(pace=http.Pace(domains=("example.com",), label="exm"))
        client.back_off(f"{SITE}/", reason="a block page", retry_after=20)
        self.assertAlmostEqual(sum(self.fake.slept), 20.0)
        # Back to the middle of that wait, as a second thread would call during it.
        self.fake.advance(-10)
        client.back_off(f"{SITE}/", reason="again")
        self.assertAlmostEqual(sum(self.fake.slept), 30.0)
        self.assertEqual(client.speeds, {"exm": 2.0})
        self.assertIn("! exm: a block page; waiting 20.0s (attempt 1)", self.screen.dest.getvalue())
        self.assertNotIn("again", self.screen.dest.getvalue())


class CallTest(ClientCase):
    def test_a_429_answer_is_waited_out_and_sent_again(self):
        answers = [Answer(429, {"retry-after": "9"}), Answer(200, {})]
        client = self.client()
        self.assertEqual(client.call(SITE, lambda: answers.pop(0)).status, 200)
        self.assertEqual((self.fake.slept, answers), ([9.0], []))

    def test_errors_are_tried_again_up_to_attempts_and_refusals_never(self):
        tries = []

        def send():
            tries.append(1)
            raise http.ServerError(503) if len(tries) < 3 else http.RefusedError(404)

        with self.assertRaises(http.RefusedError):
            self.client().call(SITE, send, attempts=3)
        self.assertEqual((len(tries), self.fake.slept), (3, [1.0, 2.0]))
        with self.assertRaises(http.ServerError):
            self.client().call(SITE, lambda: (_ for _ in ()).throw(http.ServerError(500)))

    def test_redirects_can_be_kept_as_they_are(self):
        self.answers = [httpx.Response(302, headers={"location": f"{SITE}/b"})]
        raw = httpx.Client(transport=httpx.MockTransport(self.serve), follow_redirects=True)
        self.addCleanup(raw.close)
        self.assertEqual(http.Client(raw).fetch(f"{SITE}/a", redirects=False).status, 302)


@dataclass
class Answer:
    """What a browser tab's request gives back, as far as `call` reads it."""

    status: int
    headers: dict


class DownloadTest(ClientCase):
    def test_a_download_resumes_after_its_429_is_waited_out(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.answers = [httpx.Response(429, headers={"Retry-After": "3"})]
        saved = self.client().download(f"{CDN}/v.mp4", folder + "/")
        self.assertEqual((saved.path, saved.path.read_bytes()), (Path(folder) / "v.mp4", b"ok"))
        self.assertAlmostEqual(self.gaps()[0], 3.0)

    def test_a_cdns_retry_after_is_waited_out_up_to_fifteen_minutes(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.answers = [httpx.Response(429, headers={"Retry-After": "3600"})]
        self.client().download(f"{CDN}/v.mp4", folder + "/")
        self.assertAlmostEqual(self.gaps()[0], 900.0)

    def test_a_download_stops_on_its_own_stop_not_only_the_clients(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        stop = threading.Event()
        stop.set()
        with self.assertRaises(http.StoppedError):
            self.client(stop=threading.Event()).download(f"{CDN}/v.mp4", folder + "/",
                                                         transfer=http.Transfer(stop=stop))


class ClientBehaviorTest(ClientCase):
    def test_client_fetch_stop_ends_5xx_retries(self):
        stop = threading.Event()
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            stop.set()
            return httpx.Response(503)

        raw = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(raw.close)
        with self.assertRaises(http.StoppedError):
            http.Client(raw, stop=stop).fetch(f"{SITE}/items", attempts=4)
        self.assertEqual(calls, 1)

    def test_backoff_doubles_despite_in_flight_successes(self):
        in_flight = threading.Event()
        release_200 = threading.Event()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/slow":
                in_flight.set()
                release_200.wait(2)
                return httpx.Response(200, content=b"ok")
            if request.url.path == "/429":
                return httpx.Response(429)
            return httpx.Response(200, content=b"ok")

        raw = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(raw.close)
        client = http.Client(raw)
        t = threading.Thread(target=lambda: client.fetch(f"{SITE}/slow"))
        t.start()
        in_flight.wait(2)
        host = client._host(SITE)
        # First 429 happens while /slow is in flight (epoch 0 -> 1).
        client._throttled(host, None)
        self.assertEqual(host.strikes, 1)
        # Stale 200 finishes now; strikes must NOT reset.
        release_200.set()
        t.join(2)
        self.assertEqual(host.strikes, 1)
        # Advance past first backoff and hit a second 429: strikes must double to 2.
        self.fake.advance(host.held() + 1.0)
        client._throttled(host, None)
        self.assertEqual(host.strikes, 2)

    def test_cookies_shared_live_with_jar(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("cookie", ""))
            return httpx.Response(200, headers={"Set-Cookie": "b=2; Domain=example.com; Path=/"})

        raw = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(raw.close)
        client = http.Client(raw)
        client.cookies = cookies.Jar([{"name": "a", "value": "1", "domain": "example.com"}])
        client.fetch(f"{SITE}/one")
        self.assertIn("a=1", seen[0])
        self.assertEqual(client.cookies["a"], "1")
        self.assertEqual(client.cookies["b"], "2")


if __name__ == "__main__":
    unittest.main()
