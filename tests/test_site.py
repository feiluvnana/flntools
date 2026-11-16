"""flnd/site.py's `TargetSite` and the source helpers built on a `Site` (`watch_responses`, `meta_results`).

`TargetSite` runs on a real `source_run` over `httpx.MockTransport`, with a fake browser whose tab
answers `goto` from a script; the clock is `clock.Fake`, so no wait is slept. Also
checks `FakeSite` has exactly the 13 members of `Site`. No network, no browser.
"""

import io
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import httpx

from example_source import API, CDN, Example
from fakes import FakeSite

from flnd import site as site_module
from flnd.common import auth, net, paths
from flnd.options import Options, RunShared
from flnd.site import TargetSite, shared_browser, source_run
from flnd.source import FetchError, File, Site, Target
from flnd.sources.common.feed import warm_tabs, watch_responses
from flnd.sources.common.relay import meta_results
from shared import cli, clock, cookies, http, store, ui
from shared._browser import Page

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
        # Playwright gives no response for a page that didn't load.
        if status is None:
            return None
        return SimpleNamespace(status=status, url=url, headers=self.headers.get(status, {}))


class FakeBrowser:
    def __init__(self, raw):
        self._jar = []
        self.raw, self.closed = raw, False
        self.cookies = SimpleNamespace(get=lambda: self._jar, add=self._jar.extend)

    def open(self):
        return SimpleNamespace(raw=self.raw, browser=self)

    def close(self):
        self.closed = True


class SiteCase(unittest.TestCase):
    def setUp(self):
        self.dest = io.StringIO()
        scope = ui.scope(dest=self.dest, color=False, width=400)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / ".flnd"
        self.stop = cli.Stopping(soon=threading.Event(), now=threading.Event())
        self.requests = []
        self.raw = FakeRaw([200])
        real_client = httpx.Client
        for patcher in (mock.patch.object(net.httpx, "Client",
                                          lambda *a, **kw: real_client(*a, **{**kw, "transport": httpx.MockTransport(self.serve)})),
                        mock.patch("shared._browser.open", lambda **options: FakeBrowser(self.raw))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fake = self.enterContext(clock.use(clock.Fake()))

    @property
    def now(self):
        return self.fake.now()

    def serve(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/missing"):
            return httpx.Response(404)
        if request.url.path.endswith("/private"):
            return httpx.Response(403)
        return httpx.Response(200, json={"path": request.url.path})

    def open(self, source=Example, *, state=None):
        """A `TargetSite` for `exm:example_user` on a run closed at the end of the test."""
        if state:
            with paths.table(self.home / paths.SOURCE_STATE, source.code) as db:
                db.update(state)
        opened = source_run(RunShared(home=self.home, history={}, stop=self.stop), source())
        run = opened.__enter__()
        self.addCleanup(opened.__exit__, None, None, None)
        return run, TargetSite(run, Target(source.code, "user", "example_user"))

    def lines(self):
        return self.dest.getvalue().splitlines()


class ExpiringLoginTest(SiteCase):
    """fixes #9: a login cookie that expired, or will within a day, is warned of at the run's start."""

    def save(self, **expires):
        jar = [{"name": name, "value": "secret-value", "domain": ".example.com", "path": "/", "expires": when}
               for name, when in expires.items()]
        auth.save(self.home, Example.code, cookies.Jar(jar))

    def test_soon_and_expired_login_cookies_are_named_never_their_value(self):
        now = clock.wall()
        self.save(token=now + 3600, other=now + 60)
        self.open()
        self.assertEqual(self.lines(), ["! exm: the login cookie token expires in 1h; run flnd login again soon"])
        self.save(token=now - 1000)
        self.open()
        self.assertEqual(self.lines()[-1], "! exm: the login cookie token has expired; run flnd login again")
        self.assertNotIn("secret-value", "\n".join(self.lines()))

    def test_a_login_cookie_far_from_expiry_or_for_the_session_says_nothing(self):
        for when in (clock.wall() + 30 * 24 * 3600, -1):
            self.save(token=when)
            self.open()
        self.assertEqual(self.lines(), [])


class ProtocolTest(unittest.TestCase):
    def test_fake_site_has_exactly_the_14_members(self):
        def public(kind):
            return {name for name in dir(kind) if not name.startswith("_")}

        members = {name for name in vars(Site) if not name.startswith("_")}
        self.assertEqual(len(members), 14)
        fake = FakeSite(Target("exm", "user", "example_user"))
        self.assertEqual(public(fake), members)
        self.assertEqual(public(TargetSite) | {"target", "time"}, members | {"close"})


class GotoTest(SiteCase):
    def test_a_429_backs_off_for_its_retry_after_and_loads_again(self):
        self.raw = FakeRaw([429, 200], headers={429: {"retry-after": "30"}})
        _, site = self.open()
        self.assertTrue(site.goto(PAGE))
        self.assertEqual(self.raw.loaded, [PAGE, PAGE])
        self.assertGreaterEqual(self.now, 30.0)
        self.assertTrue(any("rate limited; waiting 30.0s" in line for line in self.lines()), self.lines())

    def test_a_404_warns_and_returns_false(self):
        self.raw = FakeRaw([404])
        _, site = self.open()
        self.assertFalse(site.goto(PAGE))
        self.assertIn(f"! exm:example_user: {PAGE}: HTTP 404", self.lines())

    def test_a_page_that_did_not_load(self):
        self.raw = FakeRaw([None])
        _, site = self.open()
        self.assertFalse(site.goto(PAGE))

    def test_ctrl_c_ends_a_wait(self):
        self.raw = FakeRaw([429], headers={429: {"retry-after": "30"}})
        _, site = self.open()
        self.stop.soon.set()
        with self.assertRaises(KeyboardInterrupt):
            site.goto(PAGE)


class PaceTest(SiteCase):
    def test_fetch_and_goto_take_turns_on_one_pace_and_a_cdn_goes_at_once(self):
        _, site = self.open()
        site.fetch(f"{API}/users/example_user")
        site.goto(PAGE)
        # Example's floor is 0.5 s, plus up to 40% jitter, across its API and its pages alike.
        self.assertTrue(0.5 <= self.now <= 0.7 + 1e-9, self.now)
        before = self.now
        site.fetch(f"{CDN}/a.jpg")
        self.assertEqual(self.now, before)

    def test_the_learned_speed_scales_the_floor_and_the_scrolls(self):
        _, site = self.open()
        site.back_off("a captcha", retry_after=1)
        site.fetch(f"{API}/a")
        before = self.now
        site.fetch(f"{API}/b")
        self.assertGreaterEqual(self.now - before, 0.5)


class BackOffTest(SiteCase):
    def test_waits_and_doubles_the_learned_speed(self):
        _, site = self.open()
        site.back_off("asked to wait a few minutes")
        self.assertEqual(self.now, 5.0)
        warned = "asked to wait a few minutes"
        self.assertTrue(any(warned in line for line in self.lines()), self.lines())

    def test_a_cdns_429_never_slows_the_source(self):
        answers = [httpx.Response(429, headers={"retry-after": "5"}),
                   httpx.Response(200, content=b"x")]
        self.serve = lambda request: answers.pop(0)
        run, _ = self.open()
        net.download(run.files, File(f"{CDN}/a.7z"), self.home / "a.7z", label="a.7z")
        self.assertTrue(any("rate limited; waiting 5.0s" in line for line in self.lines()), self.lines())


class FetchTest(SiteCase):
    def test_a_response_and_soft_statuses(self):
        _, site = self.open()
        response = site.fetch(f"{API}/users/example_user", params={"count": 5})
        self.assertIsInstance(response, http.Response)
        self.assertEqual(response.json, {"path": "/users/example_user"})
        self.assertIsNone(site.try_fetch(f"{API}/missing"))
        with self.assertRaises(FetchError) as caught:
            site.fetch(f"{API}/private")
        self.assertEqual(caught.exception.status, 403)
        # The refusal keeps its response, so a source can read the site's reason from its body.
        response = caught.exception.response
        assert response is not None
        self.assertEqual(response.status, 403)
        self.assertEqual(response.url, f"{API}/private")

    def test_prepare_runs_for_the_sources_own_domains_only(self):
        jar = [{"name": "token", "value": "t0k", "domain": ".example.com", "path": "/"}]
        auth.save(self.home, "exm", cookies.Jar(jar))
        _, site = self.open()
        site.fetch(f"{API}/users/example_user")
        site.fetch(f"{CDN}/a.jpg")
        self.assertEqual([request.headers.get("authorization") for request in self.requests], ["Bearer t0k", None])

    def test_in_page_waits_out_a_429_and_tries_a_5xx_again(self):
        run, site = self.open()
        answer = http.Response(200, f"{API}/feed", {"content-type": "application/json"}, b'{"items": []}')
        errors = [http.RateLimitedError(5.0), http.Error("HTTP 503")]
        with mock.patch.object(Page, "fetch", side_effect=[*errors, answer]) as fetch:
            self.assertEqual(site.fetch(f"{API}/feed", in_page=True, params={"a": 1}).json, {"items": []})
        self.assertEqual(fetch.call_args.args[0], f"{API}/feed?a=1")

    def test_in_page_refusals_are_fetch_errors(self):
        _, site = self.open()
        refusal = http.Response(400, f"{API}/feed", {"content-type": "application/json"}, b'{"status": "fail"}')
        with mock.patch.object(Page, "fetch", side_effect=http.RefusedError(400, response=refusal)), \
                self.assertRaises(FetchError) as caught:
            site.fetch(f"{API}/feed", in_page=True)
        self.assertEqual(caught.exception.status, 400)
        response = caught.exception.response
        assert response is not None
        self.assertEqual(response.json, {"status": "fail"})
        with self.assertRaisesRegex(TypeError, "takes params, data, json and timeout"):
            site.fetch(f"{API}/feed", in_page=True, follow_redirects=False)

    def test_the_browser_opens_with_the_engines_settings_and_cookies_and_closes_with_the_target(self):
        class Wide(Example):
            stealth = False

        opened = []

        def connect(**options):
            browser: Any = FakeBrowser(self.raw)
            browser.options, browser.added = options, []
            browser.cookies = SimpleNamespace(get=lambda: browser.added, add=browser.added.extend)
            opened.append(browser)
            return browser

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
        browser = cast(FakeBrowser, opened.browser)
        site.close()
        self.assertTrue(browser.closed)


class StubRaw:
    def __init__(self):
        self.listeners = []
        self.stopped = False

    def on(self, event, listener):
        self.listeners.append(listener)

    def remove_listener(self, event, listener):
        if listener in self.listeners:
            self.listeners.remove(listener)
        self.stopped = True

    def emit(self, data):
        response = SimpleNamespace(url="https://example.com/graphql", text=lambda: json.dumps(data), request=None)
        for listener in list(self.listeners):
            listener(response)


class StubPage:
    """A tab for `watch_responses`: `scroll` delivers the next batch of responses, then the feed ends."""

    def __init__(self, batches):
        self.raw = StubRaw()
        self.batches = list(batches)
        self.scrolls = 0
        self.waits = 0

    def deliver(self):
        for data in self.batches.pop(0) if self.batches else []:
            self.raw.emit(data)

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
        site: Any = SimpleNamespace(page=page)
        responses = watch_responses(site, "graphql", scroll=True)
        page.raw.emit({"n": 1})     # the first load, which came before any scroll
        self.assertEqual([data["n"] for data in responses], [1, 2, 3, 4])
        # The last scroll found nothing new: the feed ended, and watching stopped.
        self.assertEqual((page.scrolls, page.raw.stopped), (3, True))

    def test_without_scrolling_it_waits_for_a_quiet_network_once(self):
        page = StubPage([[{"n": 2}]])
        with watch_responses(cast(Any, SimpleNamespace(page=page)), "graphql") as responses:
            page.raw.emit({"n": 1})
            self.assertEqual([data["n"] for data in responses], [1, 2])
        self.assertEqual((page.scrolls, page.waits, page.raw.stopped), (0, 1, True))


class TabRaw(FakeRaw):
    """Fake Playwright tab tracking listeners, wait states, and navigation wait_until modes."""

    def __init__(self, status=200):
        super().__init__([status] * 20)
        self.wait_until_calls = []
        self.waits = []
        self.events = {}
        self.closed = False

    def close(self):
        self.closed = True

    def on(self, event, listener):
        self.events.setdefault(event, []).append(listener)

    def remove_listener(self, event, listener):
        listeners = self.events.get(event, [])
        if listener in listeners:
            listeners.remove(listener)

    def goto(self, url, wait_until="load", timeout=30000):
        self.wait_until_calls.append((url, wait_until))
        self.loaded.append(url)
        return SimpleNamespace(status=200, url=url, headers={})

    def wait_for_load_state(self, state, timeout=30000):
        self.waits.append(state)

    def emit(self, url, body):
        """Deliver a JSON response event on this tab to active response listeners."""
        request = SimpleNamespace(is_navigation_request=lambda: False, frame=object())
        response = SimpleNamespace(url=url, headers={}, request=request, text=lambda: body)
        for listener in list(self.events.get("response", ())):
            listener(response)


class MultiTabBrowser:
    """Fake single-window `Browser` opening a distinct `TabRaw` on each `open()` call."""

    def __init__(self):
        self.opened = []
        self._jar = []
        self.cookies = SimpleNamespace(get=lambda: self._jar, add=self._jar.extend)
        self.closed = False

    def open(self):
        raw = TabRaw()
        self.opened.append(raw)
        return SimpleNamespace(raw=raw, browser=self)

    def close(self):
        self.closed = True


class TabTest(SiteCase):
    def _open_multi_browser(self):
        browsers = []

        def connect(**options):
            browser = MultiTabBrowser()
            browsers.append(browser)
            return browser

        patcher = mock.patch("shared._browser.open", connect)
        patcher.start()
        self.addCleanup(patcher.stop)
        _, site = self.open()
        self.addCleanup(site.close)
        return browsers, site

    def test_warm_tabs_preloads_in_one_window_and_rebinds_active_watch(self):
        browsers, site = self._open_multi_browser()
        urls = [f"{API}/item/{i}" for i in range(8)]
        warm_tabs(site, urls)
        self.assertEqual(len(browsers), 1)
        browser = browsers[0]
        self.assertEqual(len(browser.opened), site_module.MAX_TABS)
        warmed_calls = [call for tab in browser.opened[1:] for call in tab.wait_until_calls]
        self.assertEqual(warmed_calls, [(url, "commit") for url in urls[:site_module.MAX_TABS - 1]])

        with watch_responses(site, "graphql", scroll=False) as responses:
            self.assertTrue(site.goto(urls[0]))
            adopted = cast(TabRaw, site.page.raw)
            self.assertIn((urls[0], "commit"), adopted.wait_until_calls)
            self.assertEqual(adopted.waits, ["load"])
            adopted.emit(f"{API}/graphql", '{"n": 42}')
            self.assertEqual([item["n"] for item in responses], [42])
        # Adopting urls[0] recycled the displaced tab and queued the next URL in the same window.
        self.assertEqual(len(browsers), 1)
        self.assertEqual(len(browser.opened), site_module.MAX_TABS)
        self.assertTrue(site.goto(urls[1]))
        self.assertEqual(len(browsers), 1)

    def test_nested_scrolling_watch_opens_second_tab_while_passive_watch_shares_tab(self):
        browsers, site = self._open_multi_browser()
        main = site.page
        with watch_responses(site, "videos", scroll=True):
            self.assertIs(site.page, main)
            with watch_responses(site, "stories", scroll=False):
                self.assertIs(site.page, main)
                self.assertEqual(len(browsers[0].opened), 1)
            with watch_responses(site, "comments", scroll=True):
                nested = site.page
                self.assertIsNot(nested, main)
                self.assertEqual((len(browsers), len(browsers[0].opened)), (1, 2))
            self.assertIs(site.page, main)
            with watch_responses(site, "replies", scroll=True):
                self.assertIs(site.page, nested)
                self.assertEqual(len(browsers[0].opened), 2)
        self.assertIs(site.page, main)

    def test_shared_browser_uses_one_window_across_sources_and_replays_warm_responses(self):
        browsers = []

        class FacebookLike(Example):
            code = "fbk"

        class InstagramLike(Example):
            code = "ins"

        def connect(**options):
            browsers.append(MultiTabBrowser())
            return browsers[-1]

        settings = RunShared(home=self.home, history={}, stop=self.stop)
        with mock.patch("shared._browser.open", connect):
            with shared_browser(settings):
                def run_target(source_cls, target, warm_url):
                    with source_run(settings, source_cls()) as run:
                        site = TargetSite(run, target)
                        try:
                            warm_tabs(site, [warm_url])
                            # Simulate a response arriving while the background tab warms.
                            tab = next(t for t in browsers[0].opened if warm_url in t.loaded)
                            tab.emit(f"{API}/graphql", '{"warm": 1}')
                            with watch_responses(site, "graphql", scroll=False) as responses:
                                self.assertTrue(site.goto(warm_url))
                                self.assertEqual([item["warm"] for item in responses], [1])
                        finally:
                            site.close()

                t1 = threading.Thread(
                    target=run_target,
                    args=(FacebookLike, Target("fbk", "user", "example_user"), f"{API}/fbk"),
                )
                t2 = threading.Thread(
                    target=run_target,
                    args=(InstagramLike, Target("ins", "user", "example_user"), f"{API}/ins"),
                )
                t1.start()
                t2.start()
                t1.join()
                t2.join()
                self.assertEqual(len(browsers), 1)
                self.assertFalse(browsers[0].closed)
            self.assertTrue(browsers[0].closed)


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
