"""flnd/site.py's `TargetSite`, with the browser part it drives (`flnd.kit.browser`'s `Tabs` and `Feed`).

`TargetSite` runs on a real `source_run` over `flnd.kit.net.fake`, and on a real loop, with a fake
browser (`FakeBrowser`, patched in for `flnd.kit.browser.launch`) whose tabs answer `goto` from a script;
the clock is `clock.Fake`, so no wait is slept. Also checks `FakeSite` has exactly the members of
`Site`. No network, no browser.
"""

import asyncio
import io
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from example_source import API, CDN, Example
from fakes import FakeSite

from flnd.engine import home as _home
from flnd.engine import run as _run
from flnd.engine import transfer as _transfer
from flnd.engine.options import RunShared
from flnd.engine.site import TargetSite, shared_browser, source_run
from flnd.errors import Blocked, SourceError, UsageError
from flnd.kit import browser as _browser
from flnd.kit import clock as _time
from flnd.kit import cookies as _cookies
from flnd.kit import net as _net
from flnd.kit.browser import Page
from flnd.source import File, LoginError, Site, SiteError, Target
from flnd.ui.log import ui

PAGE = "https://www.example.com/example_user/"


class FakeRaw:
    """Playwright's page, as far as `flnd.kit.browser.Page` and `Tabs` use it: `goto` answers from `statuses`.

    Its coroutines run on the run's loop, as Playwright's would. `emit` sends a response to the tab's
    listeners; `waits` and `wait_untils` record what was waited for.
    """

    def __init__(self, statuses=(200,) * 20, headers=None):
        self.statuses, self.headers = list(statuses), headers or {}
        self.loaded, self.wait_untils, self.waits = [], [], []
        self.events = {}
        self.url = "about:blank"
        self.closed = False

    def on(self, event, listener):
        self.events.setdefault(event, []).append(listener)

    def remove_listener(self, event, listener):
        listeners = self.events.get(event, [])
        if listener in listeners:
            listeners.remove(listener)

    async def goto(self, url, wait_until="load", timeout=30000):
        self.loaded.append(url)
        self.wait_untils.append((url, wait_until))
        status = self.statuses.pop(0)
        # Playwright gives no response for a page that didn't load.
        if status is None:
            return None
        self.url = url
        return SimpleNamespace(status=status, url=url, headers=self.headers.get(status, {}))

    async def wait_for_load_state(self, state, timeout=30000):
        self.waits.append(state)

    async def close(self):
        self.closed = True

    async def emit(self, url, body):
        """Deliver one response to the tab's listeners, and let their body reads finish."""
        async def text():
            return body

        response = SimpleNamespace(url=url, request=SimpleNamespace(resource_type="xhr"), text=text)
        for listener in list(self.events.get("response", ())):
            listener(response)
        await asyncio.sleep(0)

    @property
    def listening(self):
        return bool(self.events.get("response"))


class FakeBrowser:
    """The browser `flnd.kit.browser.launch` gives, opening a real `Page` over `make_raw()` for each tab."""

    def __init__(self, make_raw, options):
        self.make_raw, self.options = make_raw, options
        self.opened, self.added = [], []
        self.headless = self.closed = False

    async def tab(self, *, by_script=False):
        raw = self.make_raw()
        self.opened.append(raw)
        return Page(raw, self, by_script=by_script)

    async def cookies(self):
        return _cookies.Jar(self.added)

    async def add_cookies(self, jar):
        self.added.extend(jar)

    async def close(self):
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
        self.stop = _run.Stop()
        self.requests = []
        self.raw = FakeRaw([200])
        # What each tab gets: this test's `raw` by default; a fresh one per tab for the tab tests.
        self.make_raw = lambda: self.raw
        self.browsers = []

        async def launch(**options):
            self.browsers.append(FakeBrowser(lambda: self.make_raw(), options))
            return self.browsers[-1]

        # A lambda, so a test that replaces `self.serve` is answered by its own.
        self.enterContext(_net.fake(lambda request: self.serve(request)))
        for patcher in (mock.patch("flnd.kit.browser.launch", launch),):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fake = self.enterContext(_time.use(_time.Fake()))

    @property
    def now(self):
        return self.fake.now

    def serve(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/missing"):
            return _net.reply(404)
        if request.url.path.endswith("/private"):
            return _net.reply(403)
        return _net.reply(200, json={"path": request.url.path})

    def open(self, source=Example, *, state=None):
        """A `TargetSite` for `exm:example_user` on a run closed at the end of the test."""
        if state:
            with _home.open(self.home) as db:
                db.state(source.code).update(state)
        opened = source_run(RunShared(home=self.home, stop=self.stop), source())
        run = opened.__enter__()
        self.addCleanup(opened.__exit__, None, None, None)
        site = TargetSite(run, Target(f"{source.code}:example_user", kind="user"))
        self.addCleanup(site.close)
        return run, site

    def lines(self):
        return self.dest.getvalue().splitlines()


class ExpiringLoginTest(SiteCase):
    """fixes #9: a login cookie that expired, or will within a day, is warned of at the run's start."""

    def save(self, **expires):
        jar = [{"name": name, "value": "secret-value", "domain": ".example.com", "path": "/", "expires": when}
               for name, when in expires.items()]
        _home.save(self.home, Example.code, _cookies.Jar(jar))

    def test_soon_and_expired_login_cookies_are_named_never_their_value(self):
        now = _time.wall()
        self.save(token=now + 3600, other=now + 60)
        self.open()
        self.assertEqual(self.lines(), ["! exm: the login cookie token expires in 1h; run flnd login exm again soon"])
        self.save(token=now - 1000)
        self.open()
        self.assertEqual(self.lines()[-1], "! exm: the login cookie token has expired; run flnd login exm again")
        self.assertNotIn("secret-value", "\n".join(self.lines()))

    def test_a_login_cookie_far_from_expiry_or_for_the_session_says_nothing(self):
        for when in (_time.wall() + 30 * 24 * 3600, -1):
            self.save(token=when)
            self.open()
        self.assertEqual(self.lines(), [])


class ProtocolTest(unittest.TestCase):
    def test_fake_site_has_exactly_the_members_of_site(self):
        def public(kind):
            return {name for name in dir(kind) if not name.startswith("_")}

        members = {name for name in vars(Site) if not name.startswith("_")}
        self.assertEqual(len(members), 26)
        fake = FakeSite(Target("exm:example_user", kind="user"))
        # `listing` is the engine's: the job sets it, for `pages` to resume; no source reads it.
        # `seen` is what a test reads back.
        self.assertEqual(public(fake) - {"listing", "seen"}, members)
        self.assertEqual(public(TargetSite) | {"target", "time"}, members | {"close"})


class OpenTest(SiteCase):
    def test_a_429_backs_off_for_its_retry_after_and_loads_again(self):
        self.raw = FakeRaw([429, 200], headers={429: {"retry-after": "30"}})
        _, site = self.open()
        self.assertTrue(site.open(PAGE))
        self.assertEqual(self.raw.loaded, [PAGE, PAGE])
        self.assertGreaterEqual(self.now, 30.0)
        self.assertTrue(any("rate limited; waiting 30.0s" in line for line in self.lines()), self.lines())

    def test_a_404_warns_and_returns_false(self):
        self.raw = FakeRaw([404])
        _, site = self.open()
        self.assertFalse(site.open(PAGE))
        self.assertIn(f"! exm:example_user: {PAGE}: HTTP 404", self.lines())

    def test_a_page_that_did_not_load(self):
        self.raw = FakeRaw([None])
        _, site = self.open()
        self.assertFalse(site.open(PAGE))

    def test_ctrl_c_ends_a_wait(self):
        self.raw = FakeRaw([429], headers={429: {"retry-after": "30"}})
        _, site = self.open()
        self.stop.soon.set()
        with self.assertRaises(KeyboardInterrupt):
            site.open(PAGE)


class PaceTest(SiteCase):
    def test_each_host_keeps_its_own_pace_and_a_first_request_goes_at_once(self):
        _, site = self.open()
        site.fetch(f"{API}/users/example_user")
        site.fetch(f"{API}/users/example_user")
        # Example's pace is 0.5 s between two requests to one host.
        self.assertEqual(self.now, 0.5)
        site.open(PAGE)
        site.fetch(f"{CDN}/a.jpg")
        self.assertEqual(self.now, 0.5)

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
        # 30 s, 2 min and 8 min for the rounds in a row; a fourth round in a row is `Blocked`.
        for waited in (30.0, 150.0, 630.0):
            site.back_off("asked to wait a few minutes")
            self.assertEqual(self.now, waited)
        warned = "asked to wait a few minutes; waiting 30.0s"
        self.assertTrue(any(warned in line for line in self.lines()), self.lines())
        with self.assertRaisesRegex(Blocked, "asked to wait a few minutes, after 3 waits"):
            site.back_off("asked to wait a few minutes")

    def test_a_quiet_spell_starts_the_rounds_again_and_a_sites_wait_is_capped(self):
        _, site = self.open()
        site.back_off("busy")
        self.fake.advance(601)
        site.back_off("busy", retry_after=7200)
        # The site's own wait wins over the round's, capped at an hour.
        self.assertEqual(self.now, 30.0 + 601 + 3600.0)

    def test_a_cdns_429_never_slows_the_source(self):
        answers = [_net.reply(429, headers={"retry-after": "5"}),
                   _net.reply(200, content=b"x")]
        self.serve = lambda request: answers.pop(0)
        run, _ = self.open()
        run.loop.run(_transfer.save(run.files, File(f"{CDN}/a.7z"), self.home / "a.7z", label="a.7z"))
        self.assertTrue(any("rate limited; waiting 5.0s" in line for line in self.lines()), self.lines())


class FetchTest(SiteCase):
    def test_a_response_and_soft_statuses(self):
        _, site = self.open()
        response = site.fetch(f"{API}/users/example_user", params={"count": 5})
        self.assertIsInstance(response, _net.Response)
        self.assertEqual(response.json, {"path": "/users/example_user"})
        self.assertEqual(site.fetch(f"{API}/missing", check=False).status, 404)
        # A 403 from the source's own domains is a login wall: `LoginError`, the refusal its cause.
        with self.assertRaises(LoginError) as walled:
            site.fetch(f"{API}/private")
        self.assertEqual((str(walled.exception), walled.exception.hint),
                         ("exm: the site refused the login (HTTP 403)", "run flnd login exm"))
        caught = walled.exception.__cause__
        assert isinstance(caught, SiteError)
        self.assertEqual(caught.status, 403)
        # The refusal keeps its response, so a source can read the site's reason from its body.
        response = caught.response
        self.assertEqual(site.fetch(f"{API}/private", check=False).status, 403)
        assert response is not None
        self.assertEqual(response.status, 403)
        self.assertEqual(response.url, f"{API}/private")

    def test_sign_runs_for_the_sources_own_domains_only(self):
        jar = [{"name": "token", "value": "t0k", "domain": ".example.com", "path": "/"}]
        _home.save(self.home, "exm", _cookies.Jar(jar))
        _, site = self.open()
        site.fetch(f"{API}/users/example_user")
        site.fetch(f"{CDN}/a.jpg")
        self.assertEqual([request.headers.get("authorization") for request in self.requests], ["Bearer t0k", None])

    def test_in_page_waits_out_a_429_and_tries_a_5xx_again(self):
        run, site = self.open()
        answer = _net.Response(200, f"{API}/feed", {"content-type": "application/json"}, b'{"items": []}')
        errors = [Blocked("HTTP 429", wait=5.0, status=429),
                  SiteError("HTTP 503", status=503, retryable=True)]
        with mock.patch.object(Page, "fetch", new=mock.AsyncMock(side_effect=[*errors, answer])) as fetch:
            self.assertEqual(site.fetch(f"{API}/feed", in_page=True, params={"a": 1}).json, {"items": []})
        self.assertEqual(fetch.call_args.args[0], f"{API}/feed?a=1")

    def test_in_page_refusals_are_fetch_errors(self):
        _, site = self.open()
        refusal = _net.Response(400, f"{API}/feed", {"content-type": "application/json"}, b'{"status": "fail"}')
        refused = mock.AsyncMock(side_effect=SiteError("HTTP 400", status=400, response=refusal))
        with mock.patch.object(Page, "fetch", new=refused), \
                self.assertRaises(SiteError) as caught:
            site.fetch(f"{API}/feed", in_page=True)
        self.assertEqual(caught.exception.status, 400)
        response = caught.exception.response
        assert response is not None
        self.assertEqual(response.json, {"status": "fail"})
        with self.assertRaisesRegex(SourceError, "takes params and body"):
            site.fetch(f"{API}/feed", in_page=True, follow_redirects=False)

    def test_the_browser_opens_with_the_engines_settings_and_cookies_and_closes_with_the_target(self):
        class Wide(Example):
            stealth = False

        _home.save(self.home, "exm",
                   _cookies.Jar([{"name": "token", "value": "t", "domain": ".example.com",
                                 "path": "/"}]))
        _, site = self.open(Wide)
        # Nothing opens until a source asks for the page.
        self.assertEqual(self.browsers, [])
        self.assertTrue(site.open(PAGE))
        [browser] = self.browsers
        # Visible (launch's default, so no headless=).
        self.assertEqual(browser.options, {"stealth": False, "headless": False})
        self.assertEqual([cookie["name"] for cookie in browser.added], ["token"])
        self.assertEqual(site.url, PAGE)
        self.assertFalse(browser.closed)
        site.close()
        self.assertTrue(browser.closed)

    def test_the_default_browser_is_stealthy(self):
        _, site = self.open()
        site.open(PAGE)
        self.assertEqual([browser.options for browser in self.browsers], [{"stealth": True, "headless": False}])

    def test_before_a_page_the_tab_is_blank_and_no_browser_starts(self):
        _, site = self.open()
        self.assertEqual(site.url, "about:blank")
        self.assertEqual(dict(site.cookies), {})
        site.close()
        self.assertEqual(self.browsers, [])

    def test_a_verb_called_from_async_code_on_the_loop_says_what_to_use(self):
        run, site = self.open()

        async def category():
            site.open(PAGE)

        with self.assertRaisesRegex(SourceError, "await"):
            run.run_async(category())

    def test_async_code_gets_the_raw_tab(self):
        run, site = self.open()

        async def category():
            return await site.tab

        self.assertIs(run.run_async(category()), self.raw)


class FeedRaw(FakeRaw):
    """A tab whose next batch of responses comes with each scroll, or with a wait for a quiet network."""

    def __init__(self, batches):
        super().__init__()
        self.batches = list(batches)
        self.scrolls = 0

    async def deliver(self):
        for data in self.batches.pop(0) if self.batches else []:
            await self.emit("https://example.com/graphql", json.dumps(data))

    async def wait_for_load_state(self, state, timeout=30000):
        await super().wait_for_load_state(state, timeout)
        await self.deliver()


class StubScroller:
    """`Page.scroller` for a `FeedRaw`: a step delivers the next batch, and the feed ends with none left.

    It keeps the options it was made with in `raw.scroller_options`.
    """

    def __init__(self, raw, options):
        self.raw, self.scrolls = raw, 0
        raw.scroller_options = options

    async def step(self):
        self.raw.scrolls += 1
        more = bool(self.raw.batches)
        await self.raw.deliver()
        return more


class FeedTest(SiteCase):
    def setUp(self):
        super().setUp()

        async def scroller(page, **options):
            return StubScroller(page.raw, options)

        patcher = mock.patch.object(Page, "scroller", scroller)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_scrolling_yields_each_response_until_the_feed_ends(self):
        self.raw = FeedRaw([[{"n": 2}], [{"n": 3}, {"n": 4}]])
        run, site = self.open()
        responses = site.feed("graphql", scroll=True)
        # The first load, which came before any scroll.
        run.loop.run(self.raw.emit("https://example.com/graphql", '{"n": 1}'))
        self.assertEqual([data["n"] for data in responses], [1, 2, 3, 4])
        # The last scroll found nothing new: the feed ended, and watching stopped.
        self.assertEqual((self.raw.scrolls, self.raw.listening), (3, False))

    def test_the_site_saying_no_more_ends_the_feed_without_another_scroll(self):
        # The second page says it is the last; the batch after it is never scrolled for.
        self.raw = FeedRaw([[{"n": 2, "next": None}], [{"n": 3, "next": "c3"}]])
        run, site = self.open()

        def cursor(page):
            return bool(page["next"]), page["next"]

        responses = site.feed("graphql", scroll=True, cursor=cursor)
        run.loop.run(self.raw.emit("https://example.com/graphql", '{"n": 1, "next": "c1"}'))
        self.assertEqual([data["n"] for data in responses], [1, 2])
        self.assertEqual((self.raw.scrolls, self.raw.listening), (1, False))
        self.assertTrue(responses.ended)

    def test_a_cursor_that_keeps_coming_back_ends_the_feed(self):
        # Once again is allowed (a page may send one request twice); a third time is the end.
        self.raw = FeedRaw([[{"n": 2}], [{"n": 3}], [{"n": 4}], [{"n": 5}]])
        _, site = self.open()
        responses = site.feed("graphql", scroll=True, cursor=lambda page: (True, "same"))
        self.assertEqual([data["n"] for data in responses], [2, 3, 4])
        self.assertEqual(self.raw.scrolls, 3)

    def test_the_feed_scrolls_in_the_sources_style_and_watches_its_capture(self):
        class Quick(Example):
            scroll = "fast"

        for source, style in ((Example, "human"), (Quick, "fast")):
            with self.subTest(style=style):
                self.raw = FeedRaw([[{"n": 1}]])
                _, site = self.open(source)
                self.assertEqual([data["n"] for data in site.feed("graphql", scroll=True)], [1])
                options = self.raw.scroller_options
                self.assertEqual(options["style"], style)
                self.assertIsInstance(options["watch"], _browser.Capture)

    def test_without_scrolling_it_waits_for_a_quiet_network_once(self):
        self.raw = FeedRaw([[{"n": 2}]])
        run, site = self.open()
        with site.feed("graphql") as responses:
            run.loop.run(self.raw.emit("https://example.com/graphql", '{"n": 1}'))
            self.assertEqual([data["n"] for data in responses], [1, 2])
        self.assertEqual((self.raw.scrolls, self.raw.waits, self.raw.listening), (0, ["networkidle"], False))

    def test_ndjson_lines_and_a_body_that_is_not_json(self):
        self.raw = FeedRaw([])
        run, site = self.open()
        with site.feed(lambda url: url.endswith("/graphql"), lines=True) as responses:
            run.loop.run(self.raw.emit("https://example.com/graphql", '{"n": 1}\n\n{"n": 2}\n'))
            run.loop.run(self.raw.emit("https://example.com/graphql", "<p>not json</p>"))
            run.loop.run(self.raw.emit("https://example.com/other", '{"n": 9}'))
            self.assertEqual([data["n"] for data in responses], [1, 2])

    def test_responses_keep_their_order_however_their_bodies_finish(self):
        run, site = self.open()
        feed = site.feed("graphql")

        async def two():
            slow_done = asyncio.Event()

            async def slow():
                await slow_done.wait()
                return '{"n": 1}'

            async def fast():
                slow_done.set()
                return '{"n": 2}'

            for text in (slow, fast):
                response = SimpleNamespace(url="https://example.com/graphql", text=text)
                for listener in list(self.raw.events["response"]):
                    listener(response)
            for _ in range(3):
                await asyncio.sleep(0)

        run.loop.run(two())
        self.assertEqual([data["n"] for data in feed], [1, 2])


class TabTest(SiteCase):
    def setUp(self):
        super().setUp()
        self.make_raw = FakeRaw

    def test_warm_preloads_in_one_window_and_moves_the_open_feed(self):
        run, site = self.open()
        urls = [f"{API}/item/{i}" for i in range(8)]
        site.warm(urls)
        [browser] = self.browsers
        self.assertEqual(len(browser.opened), _browser.MAX_TABS)
        warmed = [call for tab in browser.opened[1:] for call in tab.wait_untils]
        self.assertEqual(warmed, [(url, "commit") for url in urls[:_browser.MAX_TABS - 1]])

        with site.feed("graphql") as responses:
            self.assertTrue(site.open(urls[0]))
            adopted = site._browser().page.raw
            self.assertIn((urls[0], "commit"), adopted.wait_untils)
            self.assertEqual(adopted.waits, ["load"])
            run.loop.run(adopted.emit(f"{API}/graphql", '{"n": 42}'))
            self.assertEqual([item["n"] for item in responses], [42])
        # Taking urls[0] recycled the displaced tab and warmed the next URL in the same window.
        self.assertEqual(len(self.browsers), 1)
        self.assertEqual(len(browser.opened), _browser.MAX_TABS)
        self.assertTrue(site.open(urls[1]))
        self.assertEqual(len(self.browsers), 1)

    def test_nested_scrolling_feed_opens_a_second_tab_while_a_passive_feed_shares_the_tab(self):
        _, site = self.open()
        tabs = site._browser()
        main = tabs.page
        with site.feed("videos", scroll=True):
            self.assertIs(tabs.page, main)
            with site.feed("stories", scroll=False):
                self.assertIs(tabs.page, main)
                self.assertEqual(len(self.browsers[0].opened), 1)
            with site.feed("comments", scroll=True):
                nested = tabs.page
                self.assertIsNot(nested, main)
                self.assertEqual((len(self.browsers), len(self.browsers[0].opened)), (1, 2))
            self.assertIs(tabs.page, main)
            with site.feed("replies", scroll=True):
                self.assertIs(tabs.page, nested)
                self.assertEqual(len(self.browsers[0].opened), 2)
        self.assertIs(tabs.page, main)

    def test_shared_browser_uses_one_window_across_sources_and_replays_warm_responses(self):
        class FacebookLike(Example):
            code = "fbk"

        class InstagramLike(Example):
            code = "ins"

        settings = RunShared(home=self.home, stop=self.stop)
        failures = []
        with shared_browser(settings):
            def run_target(source_cls, target, warm_url):
                try:
                    with source_run(settings, source_cls()) as run:
                        site = TargetSite(run, target)
                        try:
                            site.warm([warm_url])
                            # A response arriving while the background tab warms.
                            tab = next(t for t in self.browsers[0].opened if warm_url in t.loaded)
                            run.loop.run(tab.emit(f"{API}/graphql", '{"warm": 1}'))
                            with site.feed("graphql") as responses:
                                self.assertTrue(site.open(warm_url))
                                self.assertEqual([item["warm"] for item in responses], [1])
                        finally:
                            site.close()
                except BaseException as error:
                    failures.append(error)

            threads = [
                threading.Thread(target=run_target,
                                 args=(FacebookLike, Target("fbk:example_user", kind="user"), f"{API}/fbk")),
                threading.Thread(target=run_target,
                                 args=(InstagramLike, Target("ins:example_user", kind="user"), f"{API}/ins")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(failures, [])
            self.assertEqual(len(self.browsers), 1)
            self.assertFalse(self.browsers[0].closed)
        self.assertTrue(self.browsers[0].closed)


class HandoffTest(SiteCase):
    def test_done_at_once_done_later_and_a_timeout(self):
        _, site = self.open()
        self.assertTrue(site.handoff(until=lambda: True, message="Solve it"))
        answers = iter([False, False, True])
        self.assertTrue(site.handoff(until=lambda: next(answers), message="Solve it"))
        self.assertEqual(self.now, 1.0)
        self.assertFalse(site.handoff(until=lambda: False, message="Solve it", timeout=5))
        self.assertGreaterEqual(self.now, 6.0)

    def test_a_headless_window_cannot_be_handed_to_a_person(self):
        _, site = self.open()
        site.open(PAGE)
        self.browsers[0].headless = True
        with self.assertRaisesRegex(UsageError, "headless"):
            site.handoff(until=lambda: False, message="Solve it")


if __name__ == "__main__":
    unittest.main()
