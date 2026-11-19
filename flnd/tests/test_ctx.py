"""The engine's `Ctx` (src/engine/ctx.py) on a real `plugin_run`, and the tabs it drives.

`CtxCase` runs over `flnkit.net.fake` and `clock.Fake`, with a fake browser (`FakeBrowser`,
patched in for `flnkit.browser.launch`) whose tabs (`FakeRaw`) answer `goto` from a script. No
network, no browser, no wait slept. tests/test_plugin.py builds on the same case.
"""

import asyncio
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from example_plugin import API, CDN, Example
from fakes import sign_in

from flnd.engine import jobs
from flnd.engine import run as _run
from flnd.engine.crawl import RunShared
from flnd.engine.ctx import Ctx, opened, plugin_run, shared_browser
from flnd.errors import NetworkError, SiteError, UsageError
from flnd.plugin import Landed, Request, Target
from flnd.ui.log import ui
from flnkit import browser as _browser
from flnkit import clock as _time
from flnkit import cookies as _cookies
from flnkit import net as _net
from flnkit.browser import Page

PAGE = "https://www.example.com/example_user/"
GRAPHQL = "https://www.example.com/graphql"


class FakeRaw:
    """Playwright's page, as far as `flnkit.browser.Page` and `Tabs` use it: `goto` answers
    from `statuses` (None: it didn't load). `emit` sends a response to the tab's listeners."""

    def __init__(self, statuses=(200,) * 20, headers=None):
        self.statuses, self.headers = list(statuses), headers or {}
        self.loaded, self.wait_untils, self.waits = [], [], []
        self.events = {}
        self.url = "about:blank"
        # The bodies read off this tab's responses.
        self.reads = 0

    def on(self, event, listener):
        self.events.setdefault(event, []).append(listener)

    def remove_listener(self, event, listener):
        if listener in self.events.get(event, []):
            self.events[event].remove(listener)

    async def goto(self, url, wait_until="load",
                   timeout=30000):  # noqa: ASYNC109 (mirrors Playwright's)
        self.loaded.append(url)
        self.wait_untils.append((url, wait_until))
        status = self.statuses.pop(0)
        if status is None:
            return None
        self.url = url
        return SimpleNamespace(status=status, url=url, headers=self.headers.get(status, {}))

    async def wait_for_load_state(self, state,
                                  timeout=30000):  # noqa: ASYNC109 (mirrors Playwright's)
        self.waits.append(state)

    async def close(self):
        pass

    async def emit(self, url, body):
        """Deliver one response to the tab's listeners, and let their body reads finish."""
        async def text():
            self.reads += 1
            return body

        self.answer(SimpleNamespace(url=url, request=SimpleNamespace(resource_type="xhr"),
                                    text=text))
        await asyncio.sleep(0)

    def answer(self, response):
        for listener in list(self.events.get("response", ())):
            listener(response)

    @property
    def listening(self):
        return bool(self.events.get("response"))


class FakeBrowser:
    """What `flnkit.browser.launch` gives: a real `Page` over `make_raw()` for each tab."""

    def __init__(self, make_raw, options):
        self.make_raw, self.options = make_raw, options
        self.opened, self.added = [], []
        self.headless = self.closed = False

    async def tab(self, *, by_script=False):
        self.opened.append(self.make_raw())
        return Page(self.opened[-1], self, by_script=by_script)

    async def cookies(self):
        return _cookies.Jar(self.added)

    async def add_cookies(self, jar):
        self.added.extend(jar)

    async def close(self):
        self.closed = True


class CtxCase(unittest.TestCase):
    """A `Ctx` for `<code>:example_user` on a `plugin_run` closed at the end of the test."""

    def setUp(self):
        self.dest = io.StringIO()
        self.enterContext(ui.scope(dest=self.dest, color=False, width=400))
        self.home = Path(self.enterContext(TemporaryDirectory())) / ".flnd"
        self.stop = _run.Stop()
        self.requests = []
        self.raw = FakeRaw([200])
        # What each tab gets: this test's `raw`, unless a test makes one per tab.
        self.make_raw = lambda: self.raw
        self.browsers = []

        async def launch(**options):
            self.browsers.append(FakeBrowser(lambda: self.make_raw(), options))
            return self.browsers[-1]

        # A lambda, so a test that replaces `self.serve` is answered by its own.
        self.enterContext(_net.fake(lambda request: self.serve(request)))
        self.enterContext(mock.patch("flnkit.browser.launch", launch))
        self.fake = self.enterContext(_time.use(_time.Fake()))

    @property
    def now(self):
        return self.fake.now

    def serve(self, request):
        self.requests.append(request)
        return _net.reply(200, json={"path": request.url.path})

    def open(self, plugin=Example):
        return self.enterContext(plugin_run(RunShared(home=self.home, stop=self.stop), plugin()))

    def ctx(self, plugin=Example, **options):
        run = self.open(plugin)
        made = Ctx(run, Target(f"{plugin.code}:example_user", kind="user"), **options)
        self.addCleanup(made.close)
        return run, made

    def call(self, run, coroutine):
        return run.loop.run(coroutine)

    def lines(self):
        return self.dest.getvalue().splitlines()


class ExpiringLoginTest(CtxCase):
    def test_a_login_cookie_near_its_end_is_named_never_its_value(self):
        """fixes #9: warned of at the run's start; one far off, or a session's, says nothing."""
        now = _time.wall()
        rows = [(now + 3600, "! exm: the login cookie token expires in 1h; run flnd auth login exm "
                             "again soon"),
                (now - 1000, "! exm: the login cookie token has expired; run flnd auth login exm "
                             "again"),
                (now + 30 * 24 * 3600, None), (-1, None)]
        for when, line in rows:
            with self.subTest(line):
                self.dest.seek(0)
                self.dest.truncate()
                jar = [{"name": name, "value": "secret-value", "domain": ".example.com",
                        "path": "/", "expires": expires}
                       for name, expires in (("token", when), ("other", now + 60))]
                sign_in(self.home, "exm", jar)
                self.open()
                self.assertEqual(self.lines(), [line] if line else [])


class OpenTest(CtxCase):
    def test_a_429_is_held_for_its_wait_and_loaded_again_and_ctrl_c_ends_the_wait(self):
        self.raw = FakeRaw([429, 200, 429], headers={429: {"retry-after": "30"}})
        run, ctx = self.ctx()
        self.assertEqual(self.call(run, ctx.open(PAGE)), Landed(PAGE, 200))
        self.assertEqual(self.raw.loaded, [PAGE, PAGE])
        self.assertGreaterEqual(self.now, 30.0)
        self.assertTrue(any("rate limited; waiting 30.0s" in line for line in self.lines()))
        self.stop.soon.set()
        with self.assertRaises(KeyboardInterrupt):
            self.call(run, ctx.open(PAGE))

    def test_a_page_that_did_not_load_is_tried_again_then_a_network_error(self):
        self.raw = FakeRaw([None] * 10)
        run, ctx = self.ctx()
        with self.assertRaisesRegex(NetworkError, "could not open"):
            self.call(run, ctx.open(PAGE))
        self.assertGreater(len(self.raw.loaded), 1)


class PaceTest(CtxCase):
    def test_a_bucket_keeps_its_pace_and_another_bucket_or_host_goes_at_once(self):
        run, ctx = self.ctx()

        async def times():
            at = []
            for url, bucket in ((f"{API}/a", None), (f"{API}/b", None),
                                (f"{API}/c", "search"), (f"{CDN}/a.jpg", None)):
                await ctx.fetch(url, bucket=bucket)
                at.append(self.now)
            return at

        # Example's pace is 0.5 s between two requests of one bucket.
        self.assertEqual(self.call(run, times()), [0.0, 0.5, 0.5, 0.5])

    def test_a_cdns_429_holds_its_host_never_the_plugin(self):
        answers = [_net.reply(429, headers={"retry-after": "5"}), _net.reply(200, content=b"x")]
        self.serve = lambda request: (answers.pop(0) if request.url.host == "cdn.example"
                                      else _net.reply(200, json={}))
        run, ctx = self.ctx()
        self.call(run, ctx.fetch(f"{CDN}/a.jpg"))
        held = self.now
        self.assertGreaterEqual(held, 5.0)
        self.call(run, ctx.fetch(f"{API}/me"))
        self.assertEqual(self.now, held)
        self.assertEqual(list(run.scheduler._holds), [("exm", None, "cdn.example")])


class FetchTest(CtxCase):
    def test_a_post_is_sent_again_after_a_429(self):
        answers = [_net.reply(429, headers={"retry-after": "3"}), _net.reply(200, json={})]
        self.serve = lambda request: (self.requests.append(request), answers.pop(0))[1]
        run, ctx = self.ctx()
        self.call(run, ctx.fetch(Request(f"{API}/form", method="POST", body={"a": 1})))
        self.assertEqual(len(self.requests), 2)

    def test_in_page_waits_out_a_429_tries_a_5xx_again_and_keeps_a_refusal(self):
        def answer(status, body=b"", **headers):
            return _net.Response(status, f"{API}/feed", {"content-type": "application/json",
                                                         **headers}, body)

        run, ctx = self.ctx()
        answers = [answer(429, **{"retry-after": "5"}), answer(503), answer(200, b'{"a": 1}'),
                   answer(400, b'{"status": "fail"}')]
        with mock.patch.object(Page, "fetch", new=mock.AsyncMock(side_effect=answers)) as sent:
            got = self.call(run, ctx.fetch(Request(f"{API}/feed", params={"a": 1}),
                                           in_page=True))
            self.assertEqual((got.json, sent.call_args.args[0]), ({"a": 1}, f"{API}/feed?a=1"))
            self.assertGreaterEqual(self.now, 5.0)
            with self.assertRaises(SiteError) as caught:
                self.call(run, ctx.fetch(f"{API}/feed", in_page=True))
        self.assertEqual((caught.exception.status, caught.exception.response.json),
                         (400, {"status": "fail"}))


    def test_in_page_from_a_blank_tab_first_loads_the_site_so_the_fetch_has_an_origin(self):
        # A tab of `aside` (a lazy post's files, the `date` hook) starts on about:blank, whose
        # null origin the site refuses: live, "TypeError: Failed to fetch".
        run, ctx = self.ctx()
        answer = _net.Response(200, f"{API}/feed", {}, b"{}")
        with mock.patch.object(Page, "fetch", new=mock.AsyncMock(return_value=answer)):
            self.call(run, ctx.fetch(f"{API}/feed", in_page=True))
            self.call(run, ctx.fetch(f"{API}/feed", in_page=True))
        raws = [raw for browser in self.browsers for raw in browser.opened]
        self.assertEqual([raw.loaded for raw in raws], [[f"{API}/robots.txt"]])


class BrowserTest(CtxCase):
    def test_it_opens_on_the_first_page_with_its_stealth_and_cookies_and_closes_with_it(self):
        class Wide(Example):
            stealth = False

        jar = [{"name": "token", "value": "t", "domain": ".example.com", "path": "/"}]
        sign_in(self.home, "exm", jar)
        for plugin, stealth in ((Example, True), (Wide, False)):
            with self.subTest(stealth=stealth):
                self.browsers.clear()
                self.raw = FakeRaw([200])
                run, ctx = self.ctx(plugin)
                self.assertEqual(self.browsers, [])
                self.call(run, ctx.open(PAGE))
                [browser] = self.browsers
                self.assertIsInstance(browser.options.pop("screen"), _browser.Screen)
                self.assertEqual(browser.options, {"stealth": stealth, "headless": False})
                self.assertEqual([cookie["name"] for cookie in browser.added], ["token"])
                ctx.close()
                self.assertTrue(browser.closed)
        self.browsers.clear()
        _, ctx = self.ctx()
        ctx.close()
        self.assertEqual(self.browsers, [])


class FeedRaw(FakeRaw):
    """A tab whose next batch of responses comes with each scroll, or with a quiet network."""

    def __init__(self, batches):
        super().__init__()
        self.batches = list(batches)
        self.scrolls = 0

    async def deliver(self):
        for data in self.batches.pop(0) if self.batches else []:
            await self.emit(GRAPHQL, json.dumps(data))

    async def wait_for_load_state(self, state,
                                  timeout=30000):  # noqa: ASYNC109 (mirrors Playwright's)
        await super().wait_for_load_state(state, timeout)
        await self.deliver()


class StubScroller:
    """`Page.scroller` for a `FeedRaw`: a step delivers the next batch; none left ends it."""

    def __init__(self, raw, options):
        self.raw = raw
        raw.scroller_options = options

    async def step(self):
        self.raw.scrolls += 1
        more = bool(self.raw.batches)
        await self.raw.deliver()
        return more


class FeedTest(CtxCase):
    def setUp(self):
        super().setUp()

        async def scroller(page, **options):
            return StubScroller(page.raw, options)

        self.enterContext(mock.patch.object(Page, "scroller", scroller))

    def read(self, batches, first=None, match="graphql", **options):
        """What `ctx.feed` yields, `first` (a body) arriving before any scroll."""
        self.raw = FeedRaw(batches)
        run, ctx = self.ctx()

        async def read():
            async with ctx.feed(match, **options) as feed:
                for url, body in first or ():
                    await self.raw.emit(url, body)
                return [item["n"] async for item in feed]

        return self.call(run, read())

    def test_it_scrolls_until_the_feed_ends_or_the_site_says_so(self):
        def cursor(page):
            return bool(page["next"]), page["next"]

        rows = [  # (name, batches, first, options, read, scrolls)
            ("its end", [[{"n": 2}], [{"n": 3}, {"n": 4}]], {"n": 1}, {}, [1, 2, 3, 4], 3),
            ("the site says no more", [[{"n": 2, "next": None}], [{"n": 3, "next": "c3"}]],
             {"n": 1, "next": "c1"}, {"cursor": cursor}, [1, 2], 1),
            # Once again is allowed (a page may send one request twice); a third time ends it.
            ("a cursor that comes back", [[{"n": 2}], [{"n": 3}], [{"n": 4}], [{"n": 5}]],
             None, {"cursor": lambda page: (True, "same")}, [2, 3, 4], 3),
            ("no scrolling", [[{"n": 2}]], {"n": 1}, {"scroll": False}, [1, 2], 0),
        ]
        for name, batches, first, options, read, scrolls in rows:
            with self.subTest(name):
                given = [(GRAPHQL, json.dumps(first))] if first else None
                self.assertEqual(self.read(batches, given, **options), read)
                scrolled = options.get("scroll", True)
                self.assertEqual((self.raw.scrolls, self.raw.listening, self.raw.waits),
                                 (scrolls, False, [] if scrolled else ["networkidle"]))
                if scrolled:
                    self.assertEqual(self.raw.scroller_options["style"], _browser.HUMAN)
                    self.assertIsInstance(self.raw.scroller_options["watch"], _browser.Capture)

    def test_ndjson_lines_and_a_body_that_is_not_json(self):
        bodies = [(GRAPHQL, '{"n": 1}\n\n{"n": 2}\n'), (GRAPHQL, "<p>not json</p>"),
                  ("https://www.example.com/other", '{"n": 9}')]
        self.assertEqual(self.read([], bodies, lambda url: url.endswith("/graphql"),
                                   lines=True, scroll=False), [1, 2])

    def test_responses_keep_their_order_however_their_bodies_finish(self):
        run, ctx = self.ctx()

        async def two():
            async with ctx.feed("graphql", scroll=False) as feed:
                slow_done = asyncio.Event()

                async def slow():
                    await slow_done.wait()
                    return '{"n": 1}'

                async def fast():
                    slow_done.set()
                    return '{"n": 2}'

                for text in (slow, fast):
                    self.raw.answer(SimpleNamespace(url=GRAPHQL, text=text))
                for _ in range(3):
                    await asyncio.sleep(0)
                return [item["n"] async for item in feed]

        self.assertEqual(self.call(run, two()), [1, 2])


class TabTest(CtxCase):
    def setUp(self):
        super().setUp()
        self.make_raw = FakeRaw

    def run_tabs(self, test):
        """`test(ctx)` on the loop of a fresh `Ctx`."""
        run, ctx = self.ctx()
        return self.call(run, test(ctx))

    def test_warm_fills_the_budget_in_one_window_and_an_open_feed_moves_to_a_warm_tab(self):
        urls = [f"https://www.example.com/item/{i}/" for i in range(8)]

        async def test(ctx):
            await ctx.warm(urls)
            [browser] = self.browsers
            # The run's 6 tabs open warm ones while 2 stay free: 4 beside the target's own.
            self.assertEqual(len(browser.opened), jobs.TABS - jobs.WARM_FREE + 1)
            warmed = [call for tab in browser.opened[1:] for call in tab.wait_untils]
            self.assertEqual(warmed, [(url, "commit") for url in urls[:4]])
            async with ctx.feed("graphql", scroll=False) as feed:
                self.assertEqual(await ctx.open(urls[0]), Landed(urls[0], 200))
                adopted = ctx._tabs._tabs.page.raw
                self.assertEqual((adopted.wait_untils, adopted.waits),
                                 ([(urls[0], "commit")], ["load"]))
                await adopted.emit(GRAPHQL, '{"n": 42}')
                self.assertEqual([item["n"] async for item in feed], [42])
            # Taking urls[0] recycled the old tab and warmed the next URL in the same window.
            self.assertEqual(len(browser.opened), jobs.TABS - jobs.WARM_FREE + 1)
            self.assertEqual(await ctx.open(urls[1]), Landed(urls[1], 200))
            self.assertEqual(len(self.browsers), 1)

        self.run_tabs(test)

    def test_warm_tabs_open_in_one_hop_and_read_only_what_an_open_feed_wants(self):
        first_url, second_url = "https://www.example.com/a/", "https://www.example.com/b/"

        async def test(ctx):
            await ctx.open(PAGE)
            shared = ctx._tabs._tabs._shared
            with mock.patch.object(shared, "run", wraps=shared.run) as hops:
                await ctx.warm([first_url, second_url])
            self.assertEqual(hops.call_count, 1)
            first, second = self.browsers[0].opened[1:]
            await first.emit(GRAPHQL, '{"n": 1}')
            await second.emit("https://www.example.com/other", '{"n": 2}')
            # No feed is open as the first is taken, and the open one wants no `other`.
            await ctx.open(first_url)
            async with ctx.feed("graphql", scroll=False):
                await ctx.open(second_url)
            self.assertEqual((first.reads, second.reads), (0, 0))

        self.run_tabs(test)

    def test_a_lazy_posts_open_takes_a_tab_of_its_own_and_an_empty_url_warms_none(self):
        post = "https://www.example.com/p/C1abc"

        async def test(ctx):
            await ctx.open(PAGE)
            listing = ctx._tabs._tabs.page.raw
            async with ctx.aside():
                await ctx.open(post)
            [browser] = self.browsers
            self.assertEqual((listing.url, browser.opened[-1].url), (PAGE, post))
            await ctx.warm(["", ""])
            self.assertEqual(len(browser.opened), 2)

        self.run_tabs(test)

    def test_a_nested_scrolling_feed_takes_a_second_tab_and_a_passive_one_shares(self):
        async def test(ctx):
            async with ctx.feed("videos"):
                tabs = ctx._tabs._tabs
                main = tabs.page
                async with ctx.feed("stories", scroll=False):
                    self.assertIs(tabs.page, main)
                    self.assertEqual(len(self.browsers[0].opened), 1)
                async with ctx.feed("comments"):
                    nested = tabs.page
                    self.assertIsNot(nested, main)
                    self.assertEqual((len(self.browsers), len(self.browsers[0].opened)), (1, 2))
                self.assertIs(tabs.page, main)
                async with ctx.feed("replies"):
                    self.assertIs(tabs.page, nested)
                    self.assertEqual(len(self.browsers[0].opened), 2)
            self.assertIs(tabs.page, main)

        self.run_tabs(test)

    def test_a_run_shares_one_window_across_plugins_and_replays_warm_responses(self):
        class First(Example):
            code = "exa"

        class Second(Example):
            code = "exb"

        settings = RunShared(home=self.home, stop=self.stop)

        async def target(plugin, url):
            async with opened(settings, plugin()) as run:
                ctx = Ctx(run, Target(f"{plugin.code}:example_user", kind="user"))
                try:
                    await ctx.warm([url])
                    # A response arriving while the background tab warms.
                    tab = next(tab for tab in self.browsers[0].opened if url in tab.loaded)
                    await tab.emit(GRAPHQL, '{"warm": 1}')
                    async with ctx.feed("graphql", scroll=False) as feed:
                        await ctx.open(url)
                        return [item["warm"] async for item in feed]
                finally:
                    await run.off(ctx.close)

        async def main():
            async with shared_browser(settings):
                read = await asyncio.gather(target(First, "https://www.example.com/a/"),
                                            target(Second, "https://www.example.com/b/"))
                return read, len(self.browsers), self.browsers[0].closed

        self.assertEqual(asyncio.run(main()), ([[1], [1]], 1, False))
        self.assertTrue(self.browsers[0].closed)


class HandoffTest(CtxCase):
    def test_done_at_once_done_later_and_a_timeout(self):
        def until(*answers):
            left = iter(answers)

            async def done():
                return next(left, False)

            return done

        run, ctx = self.ctx()
        self.assertTrue(self.call(run, ctx.handoff("Solve it", until=until(True))))
        self.assertTrue(self.call(run, ctx.handoff("Solve it", until=until(False, False, True))))
        self.assertEqual(self.now, 1.0)
        self.assertFalse(self.call(run, ctx.handoff("Solve it", until=until(), timeout=5)))
        self.assertGreaterEqual(self.now, 6.0)

    def test_a_headless_window_cannot_be_handed_to_a_person(self):
        run, ctx = self.ctx()
        self.call(run, ctx.open(PAGE))
        self.browsers[0].headless = True
        with self.assertRaisesRegex(UsageError, "headless"):
            self.call(run, ctx.handoff("Solve it", until=lambda: asyncio.sleep(0, False)))


if __name__ == "__main__":
    unittest.main()
