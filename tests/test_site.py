"""flnd/site.py's `TargetSite` and the source helpers built on a `Site` (`watch_responses`, `meta_results`).

`TargetSite` runs on a real `source_run` over `httpx.MockTransport`, with a fake browser whose tab
answers `goto` from a script; net.py's and site.py's clocks are faked, so no wait is slept. Also
checks `FakeSite` has exactly the 13 members of `Site`. No network, no browser.
"""

import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import httpx

from example_source import API, CDN, Example
from fakes import FakeSite

from flnd import site as site_module
from flnd.common import auth, files, net
from flnd.common.pace import STATE_KEY
from flnd.session import Options, Settings
from flnd.site import TargetSite, source_run
from flnd.source import FetchError, Site, Target
from flnd.sources.common.relay import meta_results
from flnd.sources.common.watch import watch_responses
from shared import cookies, http, kv, theme
from shared._browser import Page, Subscription
from shared._theme import Screen

PAGE = "https://www.example.com/example_user/"


class FakeRaw:
    """Playwright's page, as far as `Page.goto` and `TargetSite` use it: `goto` answers from `statuses`."""

    def __init__(self, statuses, headers=None):
        self.statuses, self.headers = list(statuses), headers or {}
        self.main_frame = object()
        self.listeners = []
        self.loaded = []

    def on(self, event, listener):
        self.listeners.append(listener)

    def goto(self, url, wait_until="load", timeout=30000):
        self.loaded.append(url)
        status = self.statuses.pop(0)
        request = SimpleNamespace(is_navigation_request=lambda: True, frame=self.main_frame)
        for listener in self.listeners:
            listener(SimpleNamespace(request=request, headers=self.headers.get(status, {})))
        return SimpleNamespace(status=status, url=url, headers=self.headers.get(status, {}))


class FakeBrowser:
    def __init__(self, raw):
        self.raw, self.cookies, self.closed = raw, [], False

    def add_cookies(self, jar):
        pass

    def open(self):
        return SimpleNamespace(raw=self.raw, browser=self)

    def close(self):
        self.closed = True


class SiteCase(unittest.TestCase):
    def setUp(self):
        self.screen = Screen(dest=io.StringIO(), inp=io.StringIO(""), width=400, tty=False, interactive=False)
        scope = theme.scope(screen=self.screen, color="none", unicode=True)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / ".flnd"
        self.now = 0.0
        self.requests = []
        self.raw = FakeRaw([200])
        for patcher in (mock.patch.object(net, "_base_transport", lambda: httpx.MockTransport(self.serve)),
                        mock.patch.object(net, "_sleep", self.sleep), mock.patch.object(net, "_clock", self.clock),
                        mock.patch.object(site_module, "_sleep", self.sleep),
                        mock.patch.object(site_module, "_clock", self.clock),
                        mock.patch("shared._browser.open", lambda **options: FakeBrowser(self.raw))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def sleep(self, seconds):
        self.now += seconds

    def clock(self):
        return self.now

    def serve(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/missing"):
            return httpx.Response(404)
        if request.url.path.endswith("/private"):
            return httpx.Response(403)
        return httpx.Response(200, json={"path": request.url.path})

    def open(self, source=Example, *, state=None, row=None):
        """A `TargetSite` for `exm:example_user` on a run closed at the end of the test."""
        if state:
            with kv.open(self.home / files.SOURCE_STATE, table=source.code) as store:
                store.update(state)
        opened = source_run(Settings(home=self.home, options=Options(), history={}), source())
        run = opened.__enter__()
        self.addCleanup(opened.__exit__, None, None, None)
        return run, TargetSite(run, Target(source.code, "user", "example_user"), row=row)

    def lines(self):
        return self.screen.dest.getvalue().splitlines()


class ExpiringLoginTest(SiteCase):
    """fixes #9: a login cookie that expired, or will within a day, is warned of at the run's start."""

    def save(self, **expires):
        jar = [{"name": name, "value": "secret-value", "domain": ".example.com", "path": "/", "expires": when}
               for name, when in expires.items()]
        auth.save(self.home, Example.code, cookies.Jar(jar))

    def test_soon_and_expired_login_cookies_are_named_never_their_value(self):
        self.save(token=1_000_000 + 3600, other=1_000_000 + 60)
        with mock.patch.object(site_module, "_now", lambda: 1_000_000):
            self.open()
        self.assertEqual(self.lines(), ["! exm: the login cookie token expires in 1h; run flnd login again soon"])
        self.save(token=999_000)
        with mock.patch.object(site_module, "_now", lambda: 1_000_000):
            self.open()
        self.assertEqual(self.lines()[-1], "! exm: the login cookie token has expired; run flnd login again")
        self.assertNotIn("secret-value", "\n".join(self.lines()))

    def test_a_login_cookie_far_from_expiry_or_for_the_session_says_nothing(self):
        for when in (1_000_000 + 30 * 24 * 3600, -1):
            self.save(token=when)
            with mock.patch.object(site_module, "_now", lambda: 1_000_000):
                self.open()
        self.assertEqual(self.lines(), [])


class ProtocolTest(unittest.TestCase):
    def test_fake_site_has_exactly_the_13_members(self):
        def public(kind):
            return {name for name in dir(kind) if not name.startswith("_")}

        members = {name for name in vars(Site) if not name.startswith("_")}
        self.assertEqual(len(members), 13)
        fake = FakeSite(Target("exm", "user", "example_user"))
        self.assertEqual(public(fake), members)
        self.assertEqual(public(TargetSite) | {"target"}, members | {"close"})


class GotoTest(SiteCase):
    def test_a_429_backs_off_for_its_retry_after_and_loads_again(self):
        self.raw = FakeRaw([429, 200], headers={429: {"retry-after": "30"}})
        run, site = self.open()
        self.assertTrue(site.goto(PAGE))
        self.assertEqual(self.raw.loaded, [PAGE, PAGE])
        self.assertGreaterEqual(self.now, 30.0)
        self.assertEqual((run.backoff.strikes, run.pacer.speed), (1, 2.0))

    def test_a_404_warns_and_returns_false(self):
        self.raw = FakeRaw([404])
        _, site = self.open()
        self.assertFalse(site.goto(PAGE))
        self.assertIn(f"! exm:example_user: {PAGE}: HTTP 404", self.lines())

    def test_a_page_that_did_not_load(self):
        self.raw = FakeRaw([None])
        _, site = self.open()
        self.assertFalse(site.goto(PAGE))


class PaceTest(SiteCase):
    def spy(self, run):
        return (mock.patch.object(run.pacer, "wait_turn", wraps=run.pacer.wait_turn),
                mock.patch.object(run.pacer, "record", wraps=run.pacer.record))

    def test_fetch_and_goto_wait_their_turn_and_record_and_a_cdn_url_does_neither(self):
        run, site = self.open()
        waiting, recording = self.spy(run)
        with waiting as wait_turn, recording as record:
            site.fetch(f"{API}/users/example_user")
            site.goto(PAGE)
            self.assertEqual([call.args[0] for call in wait_turn.call_args_list], [f"{API}/users/example_user", PAGE])
            self.assertEqual(record.call_count, 2)
            site.fetch(f"{CDN}/a.jpg")
            self.assertEqual((wait_turn.call_count, record.call_count), (2, 2))

    def test_requests_are_spaced_by_the_floor(self):
        _, site = self.open()
        site.fetch(f"{API}/a")
        before = self.now
        site.fetch(f"{API}/b")
        # Example's floor is 0.5 s, plus up to 40% jitter.
        self.assertGreaterEqual(self.now - before, 0.5)
        self.assertLessEqual(self.now - before, 0.7 + 1e-9)

    def test_the_learned_speed_scales_both_floors(self):
        run, site = self.open()
        site.back_off("a captcha")
        self.assertEqual((run.pacer.request_floor, run.pacer.scroll_floor), (1.0, 0.6))
        site.fetch(f"{API}/a")
        before = self.now
        site.fetch(f"{API}/b")
        self.assertGreaterEqual(self.now - before, 1.0)
        with mock.patch.object(Page, "scroll", return_value=3) as scroll, \
                mock.patch.object(run.pacer, "clean", wraps=run.pacer.clean) as clean:
            self.assertEqual(site.page.scroll(count=lambda: 0), 3)
            self.assertAlmostEqual(scroll.call_args.kwargs["pause"], 0.6)
            scroll.call_args.kwargs["on_step"]()
            clean.assert_called_once()

    def test_the_speed_is_loaded_from_state_and_saved_back(self):
        run, site = self.open(state={STATE_KEY: 4.0})
        self.assertEqual(run.pacer.speed, 4.0)
        site.back_off("throttled")
        run.close()
        with kv.open(self.home / files.SOURCE_STATE, table="exm") as store:
            self.assertEqual(store[STATE_KEY], 8.0)

    def test_a_wait_over_15_seconds_shows_on_the_row(self):
        row = SimpleNamespace(caption="")
        captions = []
        run, site = self.open(row=row)
        site.fetch(f"{API}/a")
        run.pacer.record(SimpleNamespace(headers={"x-rate-limit-remaining": "0", "x-rate-limit-reset": "30"}))
        with mock.patch.object(site_module, "_sleep", lambda seconds: captions.append(row.caption) or self.sleep(seconds)):
            site.fetch(f"{API}/b")
        self.assertEqual(len(captions), 1)
        self.assertTrue(captions[0].startswith("pacing: next request in "), captions)
        self.assertEqual(row.caption, "")


class BackOffTest(SiteCase):
    def test_waits_on_the_backoff_and_slows_the_pacer(self):
        run, site = self.open()
        site.back_off("asked to wait a few minutes")
        # The first wait is 30 s, give or take 20%.
        self.assertTrue(24.0 <= self.now <= 36.0, self.now)
        self.assertEqual((run.backoff.strikes, run.pacer.speed), (1, 2.0))
        self.assertTrue(any("asked to wait a few minutes; waiting" in line for line in self.lines()))

    def test_a_second_call_during_a_wait_adds_nothing(self):
        run, site = self.open()
        run.backoff.hit("first")
        site.back_off("second")
        self.assertEqual((run.backoff.strikes, run.pacer.speed), (1, 2.0))


class FetchTest(SiteCase):
    def test_a_response_and_soft_statuses(self):
        _, site = self.open()
        response = site.fetch(f"{API}/users/example_user", params={"count": 5})
        self.assertIsInstance(response, http.Response)
        self.assertEqual(response.json, {"path": "/users/example_user"})
        self.assertIsNone(site.fetch(f"{API}/missing", soft=True))
        with self.assertRaises(FetchError) as caught:
            site.fetch(f"{API}/private")
        self.assertEqual(caught.exception.status, 403)
        # The refusal keeps its response, so a source can read the site's reason from its body.
        self.assertEqual(caught.exception.response.status, 403)
        self.assertEqual(caught.exception.response.url, f"{API}/private")

    def test_prepare_runs_for_the_sources_own_domains_only(self):
        jar = [{"name": "token", "value": "t0k", "domain": ".example.com", "path": "/"}]
        auth.save(self.home, "exm", cookies.Jar(jar))
        _, site = self.open()
        site.fetch(f"{API}/users/example_user")
        site.fetch(f"{CDN}/a.jpg")
        self.assertEqual([request.headers.get("authorization") for request in self.requests], ["Bearer t0k", None])

    def test_in_page_waits_out_a_429_and_sends_again(self):
        run, site = self.open()
        answer = http.Response(200, f"{API}/feed", {"content-type": "application/json"}, b'{"items": []}')
        with mock.patch.object(Page, "fetch", side_effect=[http.RateLimitedError(5.0), answer]) as fetch:
            self.assertEqual(site.fetch(f"{API}/feed", in_page=True, params={"a": 1}).json, {"items": []})
        self.assertEqual(fetch.call_args.args[0], f"{API}/feed?a=1")
        self.assertEqual(run.backoff.strikes, 1)

    def test_in_page_refusals_are_fetch_errors(self):
        _, site = self.open()
        refusal = http.Response(400, f"{API}/feed", {"content-type": "application/json"}, b'{"status": "fail"}')
        with mock.patch.object(Page, "fetch", side_effect=http.RefusedError(400, response=refusal)), \
                self.assertRaises(FetchError) as caught:
            site.fetch(f"{API}/feed", in_page=True)
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.response.json, {"status": "fail"})
        with self.assertRaisesRegex(TypeError, "takes params, data, json and timeout"):
            site.fetch(f"{API}/feed", in_page=True, follow_redirects=False)

    def test_the_browser_opens_with_the_engines_settings_and_cookies_and_closes_with_the_target(self):
        class Wide(Example):
            stealth = False

        opened = []

        def connect(**options):
            opened.append(FakeBrowser(self.raw))
            opened[-1].options, opened[-1].added = options, []
            opened[-1].add_cookies = opened[-1].added.extend
            return opened[-1]

        with mock.patch("shared._browser.open", connect):
            auth.save(self.home, "exm", cookies.Jar([{"name": "token", "value": "t", "domain": ".example.com",
                                                      "path": "/"}]))
            _, site = self.open(Wide)
            # Nothing opens until a source asks for the page.
            self.assertEqual(opened, [])
            self.assertTrue(site.goto(PAGE))
            [browser] = opened
            # Visible (open's default, so no headless=), with the computer's own language and timezone.
            self.assertEqual(browser.options, {"stealth": False})
            self.assertEqual([cookie["name"] for cookie in browser.added], ["token"])
            self.assertFalse(browser.closed)
            site.close()
        self.assertTrue(browser.closed)

    def test_the_default_browser_is_stealthy(self):
        seen = []
        with mock.patch("shared._browser.open", lambda **options: seen.append(options) or FakeBrowser(self.raw)):
            _, site = self.open()
            site.goto(PAGE)
        self.assertEqual(seen, [{"stealth": True}])

    def test_close_closes_the_browser(self):
        _, site = self.open()
        opened = site.page
        browser = opened.browser
        site.close()
        self.assertTrue(browser.closed)


class StubPage:
    """A tab for `watch_responses`: `scroll` delivers the next batch of responses, then the feed ends."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.handler = None
        self.stopped = False
        self.scrolls = 0
        self.waits = 0

    def on_response(self, match, handler, *, lines=False):
        self.handler = handler

        def stop():
            self.stopped = True

        return Subscription(stop)

    def deliver(self):
        for data in self.batches.pop(0) if self.batches else []:
            self.handler(data, None)

    def scroll(self, *, until=None, **options):
        self.scrolls += 1
        self.deliver()
        return 1

    def wait(self, css=None, *, idle=False, timeout=30.0):
        self.waits += 1
        self.deliver()


class WatchTest(unittest.TestCase):
    def test_scrolling_yields_each_response_until_the_feed_ends(self):
        page = StubPage([[{"n": 2}], [{"n": 3}, {"n": 4}]])
        site = SimpleNamespace(page=page)
        responses = watch_responses(site, "graphql", scroll=True)
        page.handler({"n": 1}, None)     # the first load, which came before any scroll
        self.assertEqual([data["n"] for data in responses], [1, 2, 3, 4])
        # The last scroll found nothing new: the feed ended, and watching stopped.
        self.assertEqual((page.scrolls, page.stopped), (3, True))

    def test_without_scrolling_it_waits_for_a_quiet_network_once(self):
        page = StubPage([[{"n": 2}]])
        with watch_responses(SimpleNamespace(page=page), "graphql") as responses:
            page.handler({"n": 1}, None)
            self.assertEqual([data["n"] for data in responses], [1, 2])
        self.assertEqual((page.scrolls, page.waits, page.stopped), (0, 1, True))


class MetaResultsTest(unittest.TestCase):
    def test_the_results_a_page_embeds(self):
        block = json.dumps({"require": [{"__bbox": {"result": {"data": {"user": {"id": "42"}}}}}]})
        html = f'<html><script type="application/json">{block}</script></html>'
        site = FakeSite(Target("ins", "user", "example_user"), pages={PAGE: html})
        self.assertEqual(meta_results(site, PAGE), [{"data": {"user": {"id": "42"}}}])

    def test_a_page_that_did_not_load_gives_none(self):
        site = mock.Mock(goto=mock.Mock(return_value=False))
        self.assertEqual(meta_results(site, PAGE), [])
        site.page.document.assert_not_called()


if __name__ == "__main__":
    unittest.main()
