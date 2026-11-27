"""Cross-platform anti-ban and account-safety invariant checks for all built-in site plugins."""

from __future__ import annotations

import asyncio
import importlib
import json
import unittest

from plugin_kit import FakeCtx, FakePage, Reply, Sent, collect

from flnd.errors import Blocked, LoginError
from flnd.plugins import fbk, ins, loi, thr, tik, twt

PLUGINS = {
    "twt": twt.Twitter, "ins": ins.Instagram, "fbk": fbk.Facebook,
    "thr": thr.Threads, "tik": tik.TikTok, "loi": loi.Loibus,
}
BROWSER_PLUGINS = ("twt", "ins", "fbk", "thr", "tik")
PACED_PLUGINS = ("twt", "ins", "fbk", "thr", "tik", "loi")
FAKE_WALL = 1_700_000_000.0


class AntiBanInvariantsTest(unittest.TestCase):
    """Shared anti-ban contract checked across every site plugin."""

    def test_pacing_stealth_user_agent_and_host_limits(self) -> None:
        for code in PACED_PLUGINS:
            with self.subTest(plugin=code):
                plugin = PLUGINS[code]
                self.assertGreaterEqual(plugin.pace, 1.5)
                self.assertTrue(plugin.stealth)
                self.assertIsNone(plugin.user_agent)
        self.assertEqual(twt.Twitter.limits, {"pbs.twimg.com": 4, "video.twimg.com": 4})
        self.assertLessEqual(loi.Loibus.limits.get("api.moegoat.com", 99), 2)

    def test_browser_plugins_declare_lock_walls(self) -> None:
        for code in BROWSER_PLUGINS:
            with self.subTest(plugin=code):
                mod = importlib.import_module(f"flnd.plugins.{code}")
                walls = getattr(mod, "LOCK_WALLS", ())
                self.assertTrue(walls, f"{code} must declare LOCK_WALLS")


class TwitterHardeningTest(unittest.TestCase):
    """Extra X/Twitter anti-ban checks: lock walls, GraphQL codes, rate-limit state, caps."""

    def test_suspended_and_arkose_walls_abort_immediately(self) -> None:
        user_url = f"{twt.API}{twt.QUERIES['UserByScreenName']}/UserByScreenName"
        user_reply = Reply({"data": {"user": {"result": {
            "rest_id": "1", "legacy": {"screen_name": "example_handle"},
        }}}})
        for wall_path in ("/account/suspended", "/i/flow/arkose"):
            with self.subTest(wall=wall_path):
                ctx = FakeCtx(twt.Twitter, "twt:example_handle", routes={user_url: user_reply},
                              pages={"https://x.com/example_handle": FakePage(
                                  url=f"https://x.com{wall_path}")})
                with self.assertRaises(LoginError) as caught:
                    collect(twt.Twitter, "twt:example_handle", ctx=ctx, categories=["posts"])
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(ctx.seen.holds, [])

    def test_codes_344_and_353_abort_non_retryably(self) -> None:
        with self.assertRaises(Blocked) as daily:
            twt._raise_graphql_errors([{"code": 344, "message": "daily limit"}])
        self.assertFalse(daily.exception.retryable)
        with self.assertRaises(LoginError) as csrf:
            twt._raise_graphql_errors([{"code": 353, "message": "csrf mismatch"}])
        self.assertFalse(csrf.exception.retryable)

    def test_graph_get_records_and_respects_ratelimit_state(self) -> None:
        user_url = f"{twt.API}{twt.QUERIES['UserByScreenName']}/UserByScreenName"
        ctx = FakeCtx(twt.Twitter, "twt:example_handle")
        start = ctx.time.wall()
        reset_at = int(start) + 120
        ctx._routes[user_url] = Reply({"data": {"user": {"result": {}}}}, headers={
            "x-rate-limit-remaining": "3", "x-rate-limit-reset": str(reset_at),
        })
        graph = twt.Graph()
        asyncio.run(graph.get(ctx, "UserByScreenName", {"screen_name": "example_handle"}))
        self.assertEqual(ctx.state["twt.ratelimit"]["UserByScreenName"],
                         {"remaining": 3, "reset": reset_at})
        # Second call sees persisted low remaining and pauses past reset before fetching.
        asyncio.run(graph.get(ctx, "UserByScreenName", {"screen_name": "example_handle"}))
        self.assertGreaterEqual(ctx.time.wall() - start, 120)

    def test_search_reset_cap_and_found_cache_size(self) -> None:
        self.assertLessEqual(twt.MAX_SEARCH_RESETS, 2)
        self.assertGreaterEqual(twt.MAX_FOUND, 256)


class PlatformHardeningTest(unittest.TestCase):
    """Anti-ban hardening checks for ins, fbk, thr, tik, and loi."""

    def test_ins_extra_lock_walls_and_story_page_checks(self) -> None:
        for wall_path in ("/accounts/two_factor", "/auth_platform"):
            with self.subTest(wall=wall_path):
                ctx = FakeCtx(ins.Instagram, "ins:example_creator", pages={
                    f"{ins.SITE}/example_creator/": FakePage(url=f"{ins.SITE}{wall_path}"),
                })
                with self.assertRaises(LoginError) as caught:
                    collect(ins.Instagram, "ins:example_creator", ctx=ctx, categories=["posts"])
                self.assertFalse(caught.exception.retryable)

        body = json.dumps({"message": "checkpoint_required", "checkpoint_url": "/challenge"})
        html = f'<html><script type="application/json">{body}</script></html>'
        ctx = FakeCtx(ins.Instagram, "ins:example_creator", pages={
            f"{ins.SITE}/stories/example_creator/": FakePage(html),
        })
        with self.assertRaises(LoginError) as caught:
            asyncio.run(ins.story_page(ctx, f"{ins.SITE}/stories/example_creator/", "stories"))
        self.assertFalse(caught.exception.retryable)

    def test_fbk_more_highlights_propagates_blocked_and_read_single_checks_feed(self) -> None:
        ctx = FakeCtx(fbk.Facebook, "fbk:example.page", pages={
            f"{fbk.SITE}/example.page": FakePage(
                feeds=[Sent(f"{fbk.SITE}/api/graphql/", {"errors": [{"code": 368}]},
                            click=fbk.HIGHLIGHT_NEXT)],
                shown=[fbk.HIGHLIGHT_NEXT],
            ),
        })
        asyncio.run(ctx.open(f"{fbk.SITE}/example.page"))
        profile = {"highlights_more": True, "highlights": {}}
        with self.assertRaises(Blocked) as caught:
            asyncio.run(fbk._more_highlights(ctx, profile))
        self.assertFalse(caught.exception.retryable)

        ctx2 = FakeCtx(fbk.Facebook, "fbk:example.page", pages={
            f"{fbk.SITE}/photo/?fbid=1": FakePage(
                html='<html><script type="application/json">{"data":{}}</script></html>',
                feeds=[Sent(f"{fbk.SITE}/api/graphql/", {"errors": [{"code": 190}]})],
            ),
        })
        async def drain():
            return [p async for p in fbk.read_single(ctx2, f"{fbk.SITE}/photo/?fbid=1")]
        with self.assertRaises(LoginError) as caught2:
            asyncio.run(drain())
        self.assertFalse(caught2.exception.retryable)

    def test_thr_tab_pages_reads_both_shapes_and_checks_embedded_throttle(self) -> None:
        media_shape = {"data": {"mediaData": {"page_info": {"has_next_page": True,
                                                            "end_cursor": "c1"}}}}
        data_shape = {"data": {"data": {"page_info": {"has_next_page": True,
                                                      "end_cursor": "c2"}}}}
        self.assertEqual(thr.TAB_PAGES(media_shape), (True, "c1"))
        self.assertEqual(thr.TAB_PAGES(data_shape), (True, "c2"))

        body = json.dumps({"message": "Please wait a few minutes before you try again."})
        html = f'<html><script type="application/json">{body}</script></html>'
        ctx = FakeCtx(thr.Threads, "thr:example_creator", pages={
            f"{thr.SITE}/@example_creator": FakePage(html),
        })
        async def drain():
            return [r async for r in thr.results(ctx, f"{thr.SITE}/@example_creator", scroll=False)]
        asyncio.run(drain())
        self.assertEqual(len(ctx.seen.holds), 1)
        self.assertGreaterEqual(ctx.seen.holds[0][1], 300.0)

    def test_tik_stories_and_tab_check_fatal_status_codes(self) -> None:
        url = f"{tik.SITE}/@example_creator"
        ctx = FakeCtx(tik.TikTok, "tik:example_creator", pages={
            url: FakePage(
                feeds=[Sent(f"{tik.SITE}/api/story/item_list/", {"status_code": 8})],
            ),
        })
        with self.assertRaises(LoginError) as caught:
            collect(tik.TikTok, "tik:example_creator", ctx=ctx, categories=["stories"])
        self.assertFalse(caught.exception.retryable)

        repost_tab = '[data-e2e="repost-tab"]'
        ctx2 = FakeCtx(tik.TikTok, "tik:example_creator", pages={
            url: FakePage(
                html='<div data-e2e="repost-tab"></div>',
                feeds=[Sent(f"{tik.SITE}/api/repost/item_list/", {"status_code": 10000},
                            click=repost_tab)],
                shown=[repost_tab],
            ),
        })
        with self.assertRaises(Blocked) as caught2:
            collect(tik.TikTok, "tik:example_creator", ctx=ctx2, categories=["reposts"])
        self.assertFalse(caught2.exception.retryable)

    def test_loi_401_aborts_with_login_error(self) -> None:
        for status, body in ((401, {}), (200, {"code": "401", "message": "expired"})):
            with self.subTest(status=status, body=body):
                ctx = FakeCtx(loi.Loibus, "loi:1", routes={
                    f"{loi.API}user/info": Reply(body, status=status),
                })
                with self.assertRaises(LoginError) as caught:
                    asyncio.run(loi._answer(ctx, f"{loi.API}user/info"))
                self.assertFalse(caught.exception.retryable)


if __name__ == "__main__":
    unittest.main()
