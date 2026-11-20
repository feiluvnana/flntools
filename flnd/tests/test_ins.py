"""Instagram on plugin API 2 (`src/plugins/ins.py`), run on `FakeCtx`: no network, no browser.

The tabs are faked pages whose feeds bring the scrolled results; a lazy post's page answers the
in-page fetch of `/p/<code>/`, and the post's own page answers `open` when the HTML lacks it.
"""

import asyncio
import json
import unittest
from collections.abc import Sequence
from datetime import date
from typing import Any
from unittest import mock
from urllib.parse import parse_qsl, urlencode

import fakes

from flnd.errors import Blocked, Changed, LoginError, NotFound, SiteError, UsageError
from flnd.plugin import File, Post, Range, Request, Target
from flnd.plugins.ins import APP_ID, Instagram, best_file, code_pk, pk_stamp
from flnd.testing import FakeCtx, FakePage, Reply, Sent, check, collect
from flnkit import net as _net
from flnkit.clock import day

SITE = "https://www.instagram.com"
GRAPHQL = f"{SITE}/graphql/query"
# 2023-11-15 12:00 UTC: the same day in every time zone.
STAMP = 1700049600
DAY = "2023-11-15"
EPOCH_MS = 1314220021721
THROTTLE = {"message": "Please wait a few minutes before you try again.", "status": "fail"}
MPD = ('<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period>'
       '<AdaptationSet><Representation mimeType="video/mp4" height="1920" bandwidth="1160975">'
       "<BaseURL>https://cdn.example/1080.mp4</BaseURL></Representation></AdaptationSet>"
       '<AdaptationSet><Representation mimeType="audio/mp4" bandwidth="95480">'
       "<BaseURL>https://cdn.example/a95.mp4</BaseURL></Representation></AdaptationSet>"
       "</Period></MPD>")


def pk_at(stamp, n=1):
    """An Instagram id made at `stamp` (Unix seconds), the n-th of that millisecond."""
    return str(((stamp * 1000 - EPOCH_MS) << 23) | n)


def embedded(*results):
    """A page whose HTML embeds `results` as Instagram's server does."""
    blocks = "".join('<script type="application/json">'
                     f'{json.dumps({"require": [["x", {"__bbox": {"result": result}}]]})}</script>'
                     for result in results)
    return f"<html><body>{blocks}</body></html>"


def image(url, width=640):
    return {"original_width": width, "image_versions2": {"candidates": [
        {"url": url.replace(".jpg", "_s.jpg"), "width": 150, "height": 150},
        {"url": url, "width": width, "height": width}]}}


def video(url):
    return {**image(url.replace(".mp4", ".jpg")), "media_type": 2, "video_versions": [
        {"url": url.replace(".mp4", "_low.mp4"), "width": 480, "height": 854},
        {"url": url, "width": 720, "height": 1280}]}


def feed(code, **media):
    """A grid item that is a whole post: a feed photo, dated, at full size."""
    return {"code": code, "product_type": "feed", "taken_at": STAMP, **media}


def grid(*nodes, more=None):
    """A posts-tab result; `more` is the next cursor, "" for the last page, None for none."""
    connection: dict[str, Any] = {"edges": [{"node": node} for node in nodes]}
    if more is not None:
        connection["page_info"] = {"has_next_page": bool(more), "end_cursor": more or None}
    return {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": connection}}


def post_page(code, **media):
    return embedded({"data": {"xig_fetch_media": {"code": code, **media}}})


class Loads(Sequence):
    """A FakePage's feeds that change with each load: the first batch, then the next; the last
    batch stays."""

    def __init__(self, *batches):
        self.batches = [list(batch) for batch in batches]

    def __iter__(self):
        return iter(self.batches.pop(0) if len(self.batches) > 1 else self.batches[0])

    def __len__(self):
        return len(self.batches[0])

    def __getitem__(self, index):
        return self.batches[0][index]


def story_page(*stories):
    return embedded({"data": {"xdt_api__v1__feed__reels_media": {
        "reels_media": [{"items": list(stories)}]}}})


PROFILE = {"data": {"user": {"id": "42", "hd_profile_pic_url_info": {
    "url": "https://cdn.example/v/123_456_n.jpg?x=1"}}}}
HIGHLIGHTS = {"data": {"highlights": {"edges": [
    {"node": {"id": "highlight:9001", "title": "Trip/2026"}}]}}}
PK = {code: pk_at(STAMP, n) for n, code in enumerate(("CAR", "R1", "NOFILE", "R2", "T1"), 1)}
# Each tab of `example_creator`: whole posts are saved as listed, the rest are lazy.
TABS = {
    "": grid(feed("P1", **image("https://cdn.example/p1.jpg")),
             {"code": "CAR", "pk": PK["CAR"], "product_type": "carousel_container",
              "taken_at": STAMP, **image("https://cdn.example/c0.jpg")},
             {"code": "R1", "pk": PK["R1"], "product_type": "clips", "taken_at": STAMP},
             feed("WEB", **image("https://cdn.example/w.webp")),
             feed("BARE", **image("https://cdn.example/media/bare")),
             {"code": "NOFILE", "pk": PK["NOFILE"], "product_type": "feed", "taken_at": STAMP}),
    "reels/": {"data": {"fetch__XDTUserDict": {"clips_connection": {"edges": [
        {"node": {"media": {"code": code, "pk": PK[code], "product_type": "clips"}}}
        for code in ("R1", "R2")]}}}},
    "tagged/": {"data": {"xdt_api__v1__usertags__user_id__feed_connection": {"edges": [
        {"node": {"code": "T1", "pk": PK["T1"]}},
        {"node": feed("P1", **image("https://cdn.example/p1.jpg"))}]}}},
    "reposts/": {"data": {"fetch__XDTUserDict": {"user_reposts_timeline": {"repost_grid_items": [
        {"media": feed("RP1", **image("https://cdn.example/rp1.jpg"))},
        {"media": {"code": "RP2"}}]}}}},
}
# The pages of the lazy posts, as the in-page fetch of `/p/<code>/` answers them.
POSTS = {
    "CAR": post_page("CAR", taken_at=STAMP, carousel_media=[
        image("https://cdn.example/c0.jpg"),
        {**video("https://cdn.example/c1.mp4"), "video_dash_manifest": MPD}]),
    "R1": post_page("R1", taken_at=STAMP, **video("https://cdn.example/r1.mp4")),
    "NOFILE": post_page("NOFILE", taken_at=STAMP),
    "R2": post_page("R2", taken_at=STAMP, **video("https://cdn.example/r2.mp4")),
    "T1": post_page("T1", taken_at=STAMP, carousel_media=[
        image("https://cdn.example/t0.jpg"), image("https://cdn.example/t1.jpg")]),
    "RP2": post_page("RP2", taken_at=STAMP, **image("https://cdn.example/rp2.jpg")),
}
ROUTES = {f"{SITE}/p/{code}/": Reply(page) for code, page in POSTS.items()}
PAGES = {
    # The profile page is the posts tab: its HTML embeds the user and the first grid page.
    f"{SITE}/example_creator/": embedded(PROFILE, HIGHLIGHTS, TABS[""]),
    **{f"{SITE}/example_creator/{tab}": embedded(result)
       for tab, result in TABS.items() if tab},
    f"{SITE}/stories/example_creator/": story_page(
        {"pk": "5001", "taken_at": STAMP, **image("https://cdn.example/s.jpg")},
        # Without `taken_at`, a story has no date, so its name has none.
        {"pk": "5003_42", **video("https://cdn.example/s3.mp4")}),
    f"{SITE}/stories/highlights/9001/": embedded({"data": {
        "xdt_api__v1__feed__reels_media__connection": {"edges": [{"node": {"items": [
            {"id": "5002_42", "taken_at": STAMP, **video("https://cdn.example/h.mp4")},
            {"id": "5004", "taken_at": STAMP, **image("https://cdn.example/h2.jpg")}]}}]}}}),
}
# What every category saves, (path, key): the names saved files have always had.
SAVED = [
    ("highlights/Trip_2026/2023-11-15_5002.mp4", "highlights/Trip_2026/2023-11-15_5002.mp4"),
    ("highlights/Trip_2026/2023-11-15_5004.jpg", "highlights/Trip_2026/2023-11-15_5004.jpg"),
    ("posts/2023-11-15_BARE.jpg", "BARE_0"),
    ("posts/2023-11-15_CAR_0.jpg", "CAR_0"),
    ("posts/2023-11-15_CAR_1.mp4", "CAR_1"),
    ("posts/2023-11-15_P1.jpg", "P1_0"),
    ("posts/2023-11-15_R1.mp4", "R1_0"),
    ("posts/2023-11-15_WEB.webp", "WEB_0"),
    ("profile/123_456_n.jpg", "profile/123_456_n.jpg"),
    ("reels/2023-11-15_R2.mp4", "R2_0"),
    ("reposts/2023-11-15_RP1.jpg", "RP1_0"),
    ("reposts/2023-11-15_RP2.jpg", "RP2_0"),
    ("stories/2023-11-15_5001.jpg", "stories/2023-11-15_5001.jpg"),
    ("stories/5003.mp4", "stories/5003.mp4"),
    ("tagged/2023-11-15_T1_0.jpg", "T1_0"),
    ("tagged/2023-11-15_T1_1.jpg", "T1_1"),
]


def fake(*, routes=None, pages=None, **options):
    return FakeCtx(Instagram, routes=ROUTES if routes is None else routes,
                   pages=PAGES if pages is None else pages, **options)


def run(target="ins:example_creator", categories=None, ctx=None):
    return collect(Instagram, target, ctx=ctx or fake(), categories=categories)


def fetched(ctx):
    return [url for _, url, _ in ctx.seen.requests]


def listed(listing):
    async def take():
        return [item async for item in listing]
    return asyncio.run(take())


class RoutesTest(unittest.TestCase):
    def test_every_form(self):
        forms = [
            ("ins:me", "me", "me"), ("ins:example_user", "user", "example_user"),
            ("ins:@example_user", "user", "example_user"),
            ("https://instagram.com/example_user/?hl=en", "user", "example_user"),
            ("https://m.instagram.com/example_user/reels/", "user", "example_user"),
            ("https://www.instagram.com/me/", "user", "me"),
            ("https://www.instagram.com/p/C1a-bc_DEF/", "post", "post/C1a-bc_DEF"),
            ("https://www.instagram.com/reel/C1abcDEFghi/?igsh=1", "post", "post/C1abcDEFghi"),
            ("instagram.com/example_user/tv/C1abcDEFghi", "post", "post/C1abcDEFghi"),
            ("https://www.instagram.com/stories/example_user/31/", None, "story links"),
            ("https://www.instagram.com/accounts/login/", None, "that's not a profile"),
            ("https://www.instagram.com/direct/inbox/", None, "that's not a profile"),
            ("ins:not a name", None, None), ("https://www.instagram.com/", None, None),
        ]
        for text, kind, value in forms:
            with self.subTest(text=text):
                if kind is not None:
                    self.assertEqual(Instagram.resolve(text), Target(f"ins:{value}", kind=kind))
                elif value is not None:
                    with self.assertRaisesRegex(UsageError, f"^ins: {value}"):
                        Instagram.resolve(text)
                else:
                    self.assertIsNone(Instagram.resolve(text))

    def test_declarations(self):
        # S13: the API version is no longer shadowed by an API client on the instance.
        self.assertEqual((Instagram.api, Instagram().api), (2, 2))
        self.assertEqual(Instagram.pace, 2.0)
        self.assertEqual(Instagram.walls, ("/accounts/login", "/challenge", "/accounts/suspended"))
        route = next(known for known in Instagram.routes if known.kind == "user")
        self.assertEqual([c.name for c in route.categories if not c.own], ["tagged", "reposts"])
        self.assertEqual([c.link({"user": "example_user"}) for c in route.categories][:5], [
            "", f"{SITE}/example_user/reels/", f"{SITE}/example_user/tagged/",
            f"{SITE}/example_user/reposts/", f"{SITE}/stories/example_user/"])
        self.assertEqual(len(check(Instagram)), 4)

    def test_sign_adds_the_web_headers_only_to_api_calls(self):
        plugin = Instagram()
        plugin.claim = "hmac.claim"
        cookies = {"csrftoken": "tok"}
        signed = plugin.sign(Request(f"{SITE}/api/v1/friendships/1/following/"), cookies)
        self.assertEqual(signed.headers, {
            "X-IG-App-ID": APP_ID, "X-ASBD-ID": "129477", "X-IG-WWW-Claim": "hmac.claim",
            "X-Requested-With": "XMLHttpRequest", "Referer": f"{SITE}/", "X-CSRFToken": "tok"})
        self.assertIn("X-IG-App-ID", plugin.sign(Request(GRAPHQL), cookies).headers)
        page = Request(f"{SITE}/p/C1abcDEFghi/")
        self.assertIs(plugin.sign(page, cookies), page)


class ProfileTest(unittest.TestCase):
    def test_every_category_is_named_and_keyed_as_before(self):
        ctx = fake()
        saved = run(categories=["all"], ctx=ctx)
        self.assertEqual(sorted((item.path, item.key) for item in saved), SAVED)
        by_path = {item.path: item for item in saved}
        self.assertEqual(by_path["posts/2023-11-15_CAR_1.mp4"].file.audio,
                         "https://cdn.example/a95.mp4")
        self.assertEqual(by_path["posts/2023-11-15_P1.jpg"].post.headers, {"Referer": f"{SITE}/"})
        # S22: no media-API call; each lazy post fetches its page HTML directly, R1 once a tab.
        self.assertEqual(fetched(ctx), [f"{SITE}/p/{code}/" for code in (
            "CAR", "R1", "NOFILE", "R1", "R2", "T1", "RP2")])
        self.assertFalse(any(options["in_page"] for _, _, options in ctx.seen.requests))
        # The profile page is the posts tab, so highlights and the picture load nothing more.
        self.assertEqual(ctx.seen.visited, [
            f"{SITE}/example_creator/", f"{SITE}/example_creator/reels/",
            f"{SITE}/example_creator/tagged/", f"{SITE}/example_creator/reposts/",
            f"{SITE}/stories/example_creator/", f"{SITE}/stories/highlights/9001/"])

    def test_the_default_takes_only_the_accounts_own(self):
        folders = {item.path.split("/")[0] for item in run()}
        self.assertEqual(folders, {"posts", "reels", "stories", "highlights", "profile"})

    def test_a_lazy_post_is_dated_by_its_pk_and_skipped_by_when_with_no_request(self):
        self.assertEqual(day(pk_stamp(PK["R2"])).isoformat(), DAY)
        ctx = fake(when=Range(since=date(2023, 11, 16)))
        self.assertEqual(run(categories=["reels"], ctx=ctx), [])
        self.assertEqual(ctx.seen.requests, [])

    def test_the_story_categories_and_the_picture_read_their_pages(self):
        # 2026-11: the profile page embeds neither the user nor the highlights; its calls do.
        pages = {**PAGES, f"{SITE}/example_creator/": FakePage(
            "<html></html>", feeds=[Sent(GRAPHQL, PROFILE), Sent(GRAPHQL, HIGHLIGHTS)])}
        ctx = fake(pages=pages)
        saved = run(categories=["stories", "highlights", "profile"], ctx=ctx)
        self.assertEqual(sorted(item.path for item in saved), sorted(
            path for path, _ in SAVED if path.startswith(("stories", "highlights", "profile"))))
        # The stories page is opened directly; the profile page once, for both of the others.
        self.assertEqual(ctx.seen.visited, [
            f"{SITE}/stories/example_creator/", f"{SITE}/example_creator/",
            f"{SITE}/stories/highlights/9001/"])
        self.assertEqual((ctx.seen.requests, ctx.seen.scrolls), ([], 0))
        self.assertIn(("info", "stories: 2 stories"), ctx.seen.logs)

    def test_a_throttled_page_load_holds_and_loads_again(self):
        """A "please wait" answer on a page's own calls is a hold, never an empty page: the
        post isn't `NotFound`, and the profile's highlights and picture aren't lost."""
        single = {"data": {"xig_fetch_media": {"code": "SINGLE", "taken_at": STAMP,
                                               **image("https://cdn.example/a.jpg")}}}
        rows = [
            ("https://www.instagram.com/p/SINGLE/", None, f"{SITE}/p/SINGLE/",
             [single], [f"SINGLE/{DAY}_SINGLE.jpg"]),
            ("ins:example_creator", ["highlights", "profile"], f"{SITE}/example_creator/",
             [PROFILE, HIGHLIGHTS], [path for path, _ in SAVED
                                     if path.startswith(("highlights", "profile"))]),
        ]
        for target, categories, url, answers, paths in rows:
            with self.subTest(target=target):
                feeds = Loads([Sent(GRAPHQL, THROTTLE)], [Sent(GRAPHQL, a) for a in answers])
                ctx = fake(routes={}, pages={**PAGES, url: FakePage("<html></html>",
                                                                    feeds=feeds)})
                saved = run(target, categories, ctx=ctx)
                self.assertEqual(sorted(item.path for item in saved), paths)
                self.assertEqual([reason for reason, _, _ in ctx.seen.holds],
                                 ["Instagram asked to wait a few minutes"])
                self.assertEqual(ctx.seen.visited[:2], [url, url])
        # A post already on its page is taken at once: a throttle beside it holds nothing.
        feeds = [Sent(GRAPHQL, THROTTLE), Sent(GRAPHQL, single)]
        ctx = fake(routes={}, pages={f"{SITE}/p/SINGLE/": FakePage("<html></html>",
                                                                   feeds=feeds)})
        saved = run("https://www.instagram.com/p/SINGLE/", ctx=ctx)
        self.assertEqual([item.path for item in saved], [f"SINGLE/{DAY}_SINGLE.jpg"])
        self.assertEqual((ctx.seen.holds, ctx.seen.visited), ([], [f"{SITE}/p/SINGLE/"]))

    def test_a_story_or_highlight_page_that_fails_fails_its_category(self):
        """S18: a page that doesn't load raises, so nothing is recorded complete."""
        for url in (f"{SITE}/stories/example_creator/", f"{SITE}/stories/highlights/9001/"):
            with self.subTest(url=url):
                pages = {**PAGES, url: FakePage(status=500)}
                with self.assertRaisesRegex(SiteError, "the page didn't load"):
                    run(categories=["stories", "highlights"], ctx=fake(pages=pages))

    def test_a_storys_music_is_saved_beside_it_in_both_shapes(self):
        sticker = {"story_music_stickers": [{"music_asset_info": {
            "progressive_download_url": "https://cdn.example/m1"}}]}
        metadata = {"music_metadata": {"music_info": {"music_asset_info": {
            "progressive_download_url": "https://cdn.example/m2"}}}}
        for extra, url in ((sticker, "https://cdn.example/m1"),
                           (metadata, "https://cdn.example/m2")):
            with self.subTest(url=url):
                story = {"pk": "31", "taken_at": STAMP, **image("https://cdn.example/s.jpg"),
                         **extra}
                pages = {**PAGES, f"{SITE}/stories/example_creator/": story_page(story)}
                saved = run(categories=["stories"], ctx=fake(pages=pages))
                self.assertEqual([(item.path, item.url) for item in saved], [
                    (f"stories/{DAY}_31.jpg", "https://cdn.example/s.jpg"),
                    (f"stories/{DAY}_31_music.m4a", url)])


class TabTest(unittest.TestCase):
    def tab(self, *results, embeds=None, routes=None, pages=None, **options):
        """A posts tab whose page embeds `embeds`, and whose n-th scroll brings `results[n-1]`."""
        page = FakePage(embedded(*(embeds or [])), feeds=[
            Sent(GRAPHQL, result, scroll=n) for n, result in enumerate(results, 1)])
        return fake(pages={f"{SITE}/example_creator/": page, **(pages or {})}, routes=routes,
                    **options)

    def test_each_lazy_post_is_read_where_the_tab_lists_it(self):
        """S17: a lazy post's files come from an in-page fetch while the tab scrolls on, so
        none is put aside for after the scroll, and no page is opened for it."""
        ctx = self.tab(grid(feed("P2", **image("https://cdn.example/p2.jpg")), more=""),
                       embeds=[grid({"code": "RP2", "product_type": "feed"}, more="c1")])
        saved = run(categories=["posts"], ctx=ctx)
        self.assertEqual([item.path for item in saved],
                         [f"posts/{DAY}_RP2.jpg", f"posts/{DAY}_P2.jpg"])
        self.assertEqual(ctx.seen.visited, [f"{SITE}/example_creator/"])
        self.assertEqual(fetched(ctx), [f"{SITE}/p/RP2/"])

    def test_a_post_page_without_the_post_is_read_from_its_own_tab(self):
        """The fallback page is opened from the post's files, in a tab of its own: the
        scrolling tab keeps its page and brings P2 after it."""
        own = FakePage("<html></html>", feeds=[Sent(GRAPHQL, {"data": {"xig_fetch_media": {
            "code": "RP2", "taken_at": STAMP, **image("https://cdn.example/rp2.jpg")}}})])
        ctx = self.tab(grid(feed("P2", **image("https://cdn.example/p2.jpg")), more=""),
                       embeds=[grid({"code": "RP2", "product_type": "feed"}, more="c1")],
                       routes={f"{SITE}/p/RP2/": Reply("<html></html>")},
                       pages={f"{SITE}/p/RP2/": own})
        self.assertEqual([item.path for item in run(categories=["posts"], ctx=ctx)],
                         [f"posts/{DAY}_RP2.jpg", f"posts/{DAY}_P2.jpg"])
        self.assertEqual(ctx.seen.visited, [f"{SITE}/example_creator/", f"{SITE}/p/RP2/"])

    def test_when_post_page_does_not_embed_later_lazy_posts_open_directly(self):
        """Once `/p/<code>/` HTML proves not to embed the post, later lazy posts skip the fetch."""
        own = {code: FakePage("<html></html>", feeds=[Sent(GRAPHQL, {"data": {
            "xig_fetch_media": {"code": code, "taken_at": STAMP,
                                **image(f"https://cdn.example/{code.lower()}.jpg")}}})])
            for code in ("RP2", "RP3")}
        ctx = self.tab(
            embeds=[grid({"code": "RP2", "product_type": "feed"},
                         {"code": "RP3", "product_type": "feed"}, more="")],
            routes={f"{SITE}/p/RP2/": Reply("<html></html>"),
                    f"{SITE}/p/RP3/": Reply("<html></html>")},
            pages={f"{SITE}/p/RP2/": own["RP2"], f"{SITE}/p/RP3/": own["RP3"]})
        self.assertEqual([item.path for item in run(categories=["posts"], ctx=ctx)],
                         [f"posts/{DAY}_RP2.jpg", f"posts/{DAY}_RP3.jpg"])
        self.assertEqual(fetched(ctx), [f"{SITE}/p/RP2/"])
        self.assertEqual(ctx.seen.visited,
                         [f"{SITE}/example_creator/", f"{SITE}/p/RP2/", f"{SITE}/p/RP3/"])

    def test_a_pinned_post_keeps_its_date_and_is_marked_pinned(self):
        """Pinned posts above newer ones don't break newest-first; `--when` still drops them."""
        pinned = {"timeline_pinned_user_ids": [42], "taken_at": STAMP - 30 * 86400}
        old = feed("OLD", **pinned, **image("https://cdn.example/old.jpg"))
        lazy = {"code": "RP2", "pk": PK["T1"], "product_type": "feed",
                "clips_tab_pinned_user_ids": [42]}
        new = feed("NEW", taken_at=STAMP + 86400, **image("https://cdn.example/new.jpg"))
        embeds = [grid(old, lazy, new, more="")]
        saved = run(categories=["posts"], ctx=self.tab(embeds=embeds))
        self.assertEqual([(item.path, day(item.post.date).isoformat(), item.post.pinned)
                          for item in saved], [
            ("posts/2023-10-16_OLD.jpg", "2023-10-16", True),
            (f"posts/{DAY}_RP2.jpg", DAY, True), ("posts/2023-11-16_NEW.jpg", "2023-11-16", False)])
        ctx = self.tab(embeds=embeds, when=Range(since=date(2023, 11, 1)))
        self.assertEqual([item.post.id for item in run(categories=["posts"], ctx=ctx)],
                         ["RP2", "NEW"])

    def test_a_lazy_post_gone_fails_alone(self):
        """A lazy post whose page answers 404 is that post's failure; the tab goes on."""
        ctx = self.tab(grid(feed("P2", **image("https://cdn.example/p2.jpg")), more=""),
                       embeds=[grid({"code": "RP2", "product_type": "feed"}, more="c1")],
                       routes={f"{SITE}/p/RP2/": Reply(status=404)})
        self.assertEqual([item.path for item in run(categories=["posts"], ctx=ctx)],
                         [f"posts/{DAY}_P2.jpg"])
        self.assertEqual([(post, type(error)) for post, error in ctx.seen.failed],
                         [("RP2", NotFound)])

    def test_placeholder_null_image_is_skipped(self):
        """Instagram's rsrc.php/null.jpg placeholder is ignored, leaving valid carousel slides."""
        broken = feed("P1", product_type="carousel_container", carousel_media=[
            image("https://static.cdninstagram.com/rsrc.php/null.jpg", width=1080),
            image("https://cdn.example/real.jpg", width=1080),
        ])
        ctx = self.tab(grid(broken, more=""), embeds=[grid(broken, more="c1")], routes={})
        self.assertEqual([item.path for item in run(categories=["posts"], ctx=ctx)],
                         [f"posts/{DAY}_P1.jpg"])
        self.assertEqual(fetched(ctx), [])

        # An all-placeholder grid item with taken_at is also skipped in read_tab.
        all_null = feed("NULL0", **image("https://static.cdninstagram.com/rsrc.php/null.jpg",
                                         width=1080))
        ctx = self.tab(embeds=[grid(all_null, feed("P1", **image("https://cdn.example/p1.jpg")),
                                    more="")], routes={})
        self.assertEqual([item.path for item in run(categories=["posts"], ctx=ctx)],
                         [f"posts/{DAY}_P1.jpg"])
        self.assertEqual(fetched(ctx), [])

    def test_placeholder_only_feed_item_is_skipped_without_lazy_fetch(self):
        """Full feed items carrying only rsrc.php/null.* URLs are skipped with no lazy fetch."""
        null_img = image("https://static.cdninstagram.com/rsrc.php/null.jpg", width=1080)
        items = [
            feed("NULL1", **null_img),
            feed("NULL2", product_type="carousel_container", carousel_media=[null_img, null_img]),
            feed("P1", **image("https://cdn.example/p1.jpg")),
        ]
        ctx = fake(
            cookies={"sessionid": "99%3As%3A1"},
            routes={
                f"{SITE}/api/v1/users/web_profile_info/": Reply(
                    {"data": {"user": {"id": "42"}}}),
                f"{SITE}/api/v1/feed/user/42/": Reply({"items": items, "more_available": False}),
            },
        )
        saved = run(categories=["posts"], ctx=ctx)
        self.assertEqual([item.path for item in saved], [f"posts/{DAY}_P1.jpg"])
        self.assertEqual(fetched(ctx), [
            f"{SITE}/api/v1/users/web_profile_info/", f"{SITE}/api/v1/feed/user/42/"])

    def test_a_tab_whose_embedded_page_is_its_last_is_not_scrolled(self):
        """Every embedded result is read, the highlights after the last grid page too; then
        the tab ends without a scroll."""
        highlight = f"{SITE}/stories/highlights/9001/"
        ctx = self.tab(grid(feed("LATE", **image("https://cdn.example/l.jpg"))), embeds=[
            PROFILE, grid(feed("P1", **image("https://cdn.example/p1.jpg")), more=""),
            HIGHLIGHTS], pages={highlight: PAGES[highlight]})
        saved = run(categories=["posts", "highlights"], ctx=ctx)
        self.assertEqual([item.path for item in saved], [f"posts/{DAY}_P1.jpg"] + [
            path for path, _ in SAVED if path.startswith("highlights")])
        self.assertEqual(ctx.seen.scrolls, 0)

    def test_which_grid_items_are_whole_posts(self):
        """An image under 90% of its original width is a thumbnail, read again from its page;
        a code a later result repeats is listed once."""
        rows = [(640, 640, []), (980, 1080, []), (640, 1080, [f"{SITE}/p/RP2/"])]
        for width, original, requests in rows:
            with self.subTest(width=width, original=original):
                item = feed("RP2", **{**image("https://cdn.example/rp2.jpg", width=width),
                                      "original_width": original})
                ctx = self.tab(grid(item, more=""), embeds=[grid(item, more="c1")])
                self.assertEqual([item.path for item in run(categories=["posts"], ctx=ctx)],
                                 [f"posts/{DAY}_RP2.jpg"])
                self.assertEqual(fetched(ctx), requests)

    def test_a_lazy_post_outside_when_by_its_own_date_saves_nothing(self):
        """Its pk says the 15th, which `--when` keeps; its page says the 14th, which it drops."""
        page = post_page("RP2", taken_at=STAMP - 86400, **image("https://cdn.example/rp2.jpg"))
        ctx = self.tab(embeds=[grid({"code": "RP2", "pk": PK["T1"], "product_type": "feed"},
                                    more="")],
                       routes={f"{SITE}/p/RP2/": Reply(page)}, when=Range(since=date(2023, 11, 15)))
        self.assertEqual(run(categories=["posts"], ctx=ctx), [])
        self.assertEqual(fetched(ctx), [f"{SITE}/p/RP2/"])

    def test_a_please_wait_answer_holds_and_the_same_post_is_saved(self):
        throttles = [Reply(THROTTLE)]

        def page(request):
            return throttles.pop() if throttles else Reply(POSTS["RP2"])

        ctx = self.tab(THROTTLE, grid(feed("P2", **image("https://cdn.example/p2.jpg")), more=""),
                       embeds=[grid({"code": "RP2", "product_type": "feed"}, more="c1")],
                       routes={f"{SITE}/p/RP2/": page})
        saved = run(categories=["posts"], ctx=ctx)
        self.assertEqual([item.path for item in saved],
                         [f"posts/{DAY}_RP2.jpg", f"posts/{DAY}_P2.jpg"])
        # One hold for the fetch's answer (blocked), one for the feed's (ctx.hold).
        [fetch, scroll] = [reason for reason, _, _ in ctx.seen.holds]
        self.assertTrue(fetch.startswith("ins: blocked, HTTP 200"), fetch)
        self.assertEqual(scroll, "Instagram asked to wait a few minutes")

    def test_while_blocked_no_post_page_opens(self):
        """S3: a lazy post refused for good raises; it never opens its page instead."""
        ctx = self.tab(embeds=[grid({"code": "RP2", "product_type": "feed"}, more="")],
                       routes={f"{SITE}/p/RP2/": Reply(THROTTLE)})
        with self.assertRaises(Blocked):
            run(categories=["posts"], ctx=ctx)
        self.assertEqual(ctx.seen.visited, [f"{SITE}/example_creator/"])

    def test_a_dash_video_keeps_its_mp4_as_a_mirror_without_the_audio(self):
        """S9: the audio joins only the DASH video, so the MP4 saves alone when it fails."""
        found = best_file({**video("https://cdn.example/r.mp4"), "video_dash_manifest": MPD})
        self.assertEqual(found, (("https://cdn.example/1080.mp4", "https://cdn.example/r.mp4"),
                                 "mp4", "https://cdn.example/a95.mp4"))
        [saved] = run(categories=["posts"], ctx=self.tab(embeds=[grid(
            {"code": "D1", "product_type": "clips", "taken_at": STAMP, "video_dash_manifest": MPD,
             **video("https://cdn.example/r.mp4")}, more="")]))
        self.assertEqual((saved.file.url, saved.file.mirrors, saved.file.audio), (
            None, ("https://cdn.example/1080.mp4", "https://cdn.example/r.mp4"),
            "https://cdn.example/a95.mp4"))

    def test_a_webm_dash_video_is_saved_as_webm(self):
        webm = MPD.replace("video/mp4", "video/webm").replace("1080.mp4", "1080.webm")
        found = best_file({"video_dash_manifest": webm})
        self.assertEqual(found, (("https://cdn.example/1080.webm",), "webm",
                                 "https://cdn.example/a95.mp4"))

    def test_logged_in_posts_reads_user_feed_api_without_opening_post_pages(self):
        """When logged in, `posts` reads `/api/v1/feed/user/<id>/` with no `/p/*` loads."""
        car = {"code": "CAR", "pk": PK["CAR"], "product_type": "carousel_container",
               "taken_at": STAMP, "carousel_media": [
                   image("https://cdn.example/c0.jpg"),
                   {**video("https://cdn.example/c1.mp4"), "video_dash_manifest": MPD}]}
        feed_pages = {
            None: Reply({"items": [feed("P1", **image("https://cdn.example/p1.jpg"))],
                         "more_available": True, "next_max_id": "m1"},
                        headers={"x-ig-set-www-claim": "c1"}),
            "m1": Reply({"items": [car], "more_available": False}),
        }
        ctx = fake(
            cookies={"sessionid": "99%3As%3A1"},
            routes={
                f"{SITE}/api/v1/users/web_profile_info/": Reply(
                    {"data": {"user": {"id": "42"}}}),
                f"{SITE}/api/v1/feed/user/42/": lambda req: feed_pages[
                    (req.params or {}).get("max_id")],
            },
        )
        saved = run(categories=["posts"], ctx=ctx)
        self.assertEqual([item.path for item in saved], [
            f"posts/{DAY}_P1.jpg", f"posts/{DAY}_CAR_0.jpg", f"posts/{DAY}_CAR_1.mp4"])
        self.assertEqual(ctx.seen.visited, [])
        self.assertFalse(any("/p/" in url for url in fetched(ctx)))

    def test_refused_user_feed_api_falls_back_to_tab_once(self):
        """A non-JSON/refused user feed API falls back to scrolling the tab for the run."""
        info_routes = {
            f"{SITE}/api/v1/media/{PK['CAR']}/info/": Reply({"items": [{
                "code": "CAR", "taken_at": STAMP, "carousel_media": [
                    image("https://cdn.example/c0.jpg"),
                    {**video("https://cdn.example/c1.mp4"), "video_dash_manifest": MPD}]}]}),
            f"{SITE}/api/v1/media/{PK['R1']}/info/": Reply({"items": [{
                "code": "R1", "taken_at": STAMP, **video("https://cdn.example/r1.mp4")}]}),
            f"{SITE}/api/v1/media/{PK['NOFILE']}/info/": Reply({"items": [{
                "code": "NOFILE", "taken_at": STAMP}]}),
        }
        ctx = fake(
            cookies={"sessionid": "99%3As%3A1"},
            routes={**info_routes,
                    f"{SITE}/api/v1/users/web_profile_info/": Reply("<html></html>")},
            pages=PAGES,
        )
        saved = collect(Instagram, "ins:example_creator", ctx=ctx, categories=["posts"])
        self.assertTrue(any(item.path == f"posts/{DAY}_P1.jpg" for item in saved))
        self.assertTrue(ctx._plugin.feed_refused)
        self.assertIn(f"{SITE}/example_creator/", ctx.seen.visited)
        self.assertFalse(any("/p/" in url for url in fetched(ctx)))

    def test_captured_graphql_post_query_replays_for_later_posts(self):
        """Once `opened_post` captures a `POST /graphql/query`, later posts replay it."""
        def gql_request(code):
            return Request(GRAPHQL, method="POST", headers={
                "content-type": "application/x-www-form-urlencoded",
                "x-fb-friendly-name": "PolarisPostRootQuery",
                "x-fb-lsd": "lsd-tok",
            }, body=urlencode({
                "fb_dtsg": "dtsg-tok", "lsd": "lsd-tok", "doc_id": "123456",
                "variables": json.dumps({"shortcode": code}),
            }))

        def gql_reply(req):
            form = dict(parse_qsl(req.body or ""))
            code = json.loads(form["variables"])["shortcode"]
            if code == "STALE":
                return Reply({"errors": [{"message": "stale doc_id"}]})
            return Reply({"data": {"xdt_api__v1__media__shortcode__web_info": {"items": [{
                "code": code, "taken_at": STAMP,
                **image(f"https://cdn.example/{code.lower()}.jpg")}]}}})

        pages = {
            f"{SITE}/example_creator/": FakePage(embedded(grid(
                {"code": "RP2", "product_type": "feed"},
                {"code": "RP3", "product_type": "feed"},
                {"code": "STALE", "product_type": "feed"}, more=""))),
            f"{SITE}/p/RP2/": FakePage("<html></html>", feeds=[Sent(
                GRAPHQL, {"data": {"xig_fetch_media": {
                    "code": "RP2", "taken_at": STAMP, **image("https://cdn.example/rp2.jpg")}}},
                request=gql_request("RP2"))]),
            f"{SITE}/p/STALE/": FakePage("<html></html>", feeds=[Sent(
                GRAPHQL, {"data": {"xig_fetch_media": {
                    "code": "STALE", "taken_at": STAMP, **image("https://cdn.example/s.jpg")}}},
                request=gql_request("STALE"))]),
        }
        ctx = fake(routes={f"{SITE}/p/RP2/": Reply("<html></html>"), GRAPHQL: gql_reply},
                   pages=pages)
        saved = run(categories=["posts"], ctx=ctx)
        self.assertEqual([item.path for item in saved], [
            f"posts/{DAY}_RP2.jpg", f"posts/{DAY}_RP3.jpg", f"posts/{DAY}_STALE.jpg"])
        self.assertEqual(ctx.seen.visited, [f"{SITE}/example_creator/", f"{SITE}/p/RP2/",
                                            f"{SITE}/p/STALE/"])
        self.assertEqual(fetched(ctx), [f"{SITE}/p/RP2/", GRAPHQL, GRAPHQL])

    def test_polaris_timeline_and_xig_polaris_media_embeds_are_read(self):
        """2026-11 public pages embed `xig_user_by_username` and `xig_polaris_media`."""
        profile_embed = {"data": {"xig_user_by_username": {
            "pk": "6074", "profile_pic_url": "https://cdn.example/pic.jpg"}}}
        grid_embed = {"data": {"xig_user_by_username": {
            "pk": "6074",
            "polaris_ordered_timeline_connection": {
                "edges": [{"node": {"code": "POL1", "pk": PK["T1"], "product_type": "feed"}}],
                "page_info": {"has_next_page": False, "end_cursor": None},
            },
        }}}
        pol1 = embedded({"data": {"xig_polaris_media": {
            "code": "POL1",
            "if_not_gated_logged_out": {"code": "POL1", "taken_at": STAMP,
                                        **image("https://cdn.example/pol1.jpg")},
        }}})
        ctx = fake(
            pages={f"{SITE}/example_creator/": FakePage(embedded(profile_embed, grid_embed))},
            routes={f"{SITE}/p/POL1/": Reply(pol1)},
        )
        saved = run(categories=["posts", "profile"], ctx=ctx)
        self.assertEqual(sorted(item.path for item in saved),
                         [f"posts/{DAY}_POL1.jpg", "profile/pic.jpg"])
        self.assertEqual(ctx.seen.visited, [f"{SITE}/example_creator/"])
        self.assertEqual(fetched(ctx), [f"{SITE}/p/POL1/"])


class PostTest(unittest.TestCase):
    def test_one_post_from_its_page_or_its_calls(self):
        carousel = {"code": "SINGLE", "taken_at": STAMP, "carousel_media": [
            image("https://cdn.example/a.jpg"), video("https://cdn.example/r.mp4")]}
        shortcode = embedded({"data": {"xdt_api__v1__media__shortcode__web_info": {
            "items": [carousel]}}})
        called = FakePage("<html></html>",
                          feeds=[Sent(GRAPHQL, {"data": {"xig_fetch_media": carousel}})])
        for page in (shortcode, called):
            with self.subTest(page=type(page).__name__):
                ctx = fake(routes={}, pages={f"{SITE}/p/SINGLE/": page})
                saved = run("https://www.instagram.com/p/SINGLE/", ctx=ctx)
                self.assertEqual([(item.path, item.key) for item in saved], [
                    (f"SINGLE/{DAY}_SINGLE_0.jpg", "SINGLE_0"),
                    (f"SINGLE/{DAY}_SINGLE_1.mp4", "SINGLE_1")])
                self.assertEqual((ctx.seen.requests, ctx.seen.scrolls), ([], 0))

    def test_a_page_without_the_post_is_not_found(self):
        """S3: no post, another post's code, or a post without files is `NotFound`."""
        other = post_page("OTHER", taken_at=STAMP, **image("https://cdn.example/o.jpg"))
        for html in ("<html></html>", other, post_page("SINGLE", taken_at=STAMP)):
            with self.subTest(html=html[:40]):
                ctx = fake(routes={}, pages={f"{SITE}/p/SINGLE/": html})
                with self.assertRaises(NotFound):
                    run("https://www.instagram.com/p/SINGLE/", ctx=ctx)

    def test_logged_in_post_uses_media_info_and_never_fetches_html(self):
        """Logged-in `post` decodes shortcode -> pk and calls `/api/v1/media/{pk}/info/`."""
        self.assertEqual(code_pk("B"), "1")
        self.assertEqual(code_pk("B-_"), str((1 << 12) | (62 << 6) | 63))
        self.assertIsNone(code_pk("bad/code"))
        self.assertIsNone(code_pk("B" * 38))
        self.assertIsNone(code_pk("Q" + "A" * 10))
        pk = code_pk("C1abc-DEF_0")
        ctx = fake(
            cookies={"sessionid": "99%3As%3A1"},
            routes={f"{SITE}/api/v1/media/{pk}/info/": Reply(
                {"items": [{"code": "C1abc-DEF_0", "taken_at": STAMP,
                            **image("https://cdn.example/c1.jpg")}]},
                headers={"x-ig-set-www-claim": "hmac.next"})},
        )
        saved = run("https://www.instagram.com/p/C1abc-DEF_0/", ctx=ctx)
        self.assertEqual([item.path for item in saved], [f"C1abc-DEF_0/{DAY}_C1abc-DEF_0.jpg"])
        self.assertEqual(ctx.seen.visited, [])
        self.assertEqual(fetched(ctx), [f"{SITE}/api/v1/media/{pk}/info/"])
        self.assertTrue(ctx.seen.requests[0][2]["in_page"])
        self.assertEqual(ctx._plugin.claim, "hmac.next")

    def test_logged_in_media_info_not_found_and_refusal_fallback(self):
        """Empty items/404 raise `NotFound`; non-JSON refusal falls back to tab and GraphQL."""
        pk_single = code_pk("SINGLE")
        for reply in (Reply(status=404), Reply({"items": []}),
                      Reply({"items": [{"code": "OTHER", "taken_at": STAMP,
                                        **image("https://cdn.example/o.jpg")}]})):
            with self.subTest(status=reply.status):
                ctx = fake(cookies={"sessionid": "99%3As%3A1"},
                           routes={f"{SITE}/api/v1/media/{pk_single}/info/": reply})
                with self.assertRaises(NotFound):
                    run("https://www.instagram.com/p/SINGLE/", ctx=ctx)
                self.assertFalse(ctx._plugin.media_info_refused)
                self.assertEqual(ctx.seen.visited, [])

        # When media info is refused, logged-in fallback skips raw GET /p/ and goes to
        # opened_post -> captured GraphQL replay for the next post.
        def gql_request(code):
            return Request(GRAPHQL, method="POST", headers={
                "content-type": "application/x-www-form-urlencoded",
                "x-fb-friendly-name": "PolarisPostRootQuery",
            }, body=urlencode({"doc_id": "123", "variables": json.dumps({"shortcode": code})}))

        def gql_reply(req):
            code = json.loads(dict(parse_qsl(req.body or ""))["variables"])["shortcode"]
            return Reply({"data": {"xdt_api__v1__media__shortcode__web_info": {"items": [{
                "code": code, "taken_at": STAMP,
                **image(f"https://cdn.example/{code.lower()}.jpg")}]}}})

        pages = {
            f"{SITE}/example_creator/reels/": FakePage(embedded({"data": {"fetch__XDTUserDict": {
                "clips_connection": {"edges": [
                    {"node": {"media": {"code": "RP2", "pk": PK["R1"],
                                        "product_type": "clips"}}},
                    {"node": {"media": {"code": "RP3", "pk": PK["R2"],
                                        "product_type": "clips"}}},
                ]}}}})),
            f"{SITE}/p/RP2/": FakePage("<html></html>", feeds=[Sent(
                GRAPHQL, {"data": {"xig_fetch_media": {
                    "code": "RP2", "taken_at": STAMP, **image("https://cdn.example/rp2.jpg")}}},
                request=gql_request("RP2"))]),
        }
        ctx = fake(
            cookies={"sessionid": "99%3As%3A1"},
            routes={f"{SITE}/api/v1/media/{PK['R1']}/info/": Reply("<html></html>"),
                    GRAPHQL: gql_reply},
            pages=pages,
        )
        saved = run(categories=["reels"], ctx=ctx)
        self.assertEqual([item.path for item in saved],
                         [f"reels/{DAY}_RP2.jpg", f"reels/{DAY}_RP3.jpg"])
        self.assertTrue(ctx._plugin.media_info_refused)
        self.assertEqual(fetched(ctx), [f"{SITE}/api/v1/media/{PK['R1']}/info/", GRAPHQL])
        self.assertEqual(ctx.seen.visited, [f"{SITE}/example_creator/reels/", f"{SITE}/p/RP2/"])

    def test_a_post_link_is_filed_by_its_owner_and_kind(self):
        rows = [("https://www.instagram.com/p/SINGLE/", {}, "example_creator/posts"),
                ("https://www.instagram.com/reel/SINGLE/", {"product_type": "clips"},
                 "example_creator/reels"),
                # No owner in the data: the post's id names the folder, as before.
                ("https://www.instagram.com/p/SINGLE/", {"user": {}}, "SINGLE")]
        for link, extra, folder in rows:
            with self.subTest(link=link, extra=extra):
                media = {"code": "SINGLE", "taken_at": STAMP, **image("https://cdn.example/a.jpg"),
                         "user": {"username": "example_creator"}, **extra}
                feeds = [Sent(GRAPHQL, {"data": {"xig_fetch_media": media}})]
                ctx = fake(routes={}, pages={f"{SITE}/p/SINGLE/": FakePage("", feeds=feeds)})
                [found] = run(link, ctx=ctx)
                self.assertEqual(fakes.filed(Instagram, link, found), folder)


class MeTest(unittest.TestCase):
    URL = f"{SITE}/api/v1/friendships/{{}}/following/"

    def follows(self, cookies, routes=None):
        """The targets `ins:me` yields on a FakeCtx with these cookies, and the ctx."""
        plugin = Instagram()
        ctx = FakeCtx(plugin, "ins:me", cookies=cookies, routes=routes)
        return listed(plugin.me(ctx, "me")), ctx

    def test_the_follows_page_by_page_from_the_tab(self):
        answers = {None: Reply({"users": [{"username": "example_user"}], "next_max_id": "1"},
                               headers={"x-ig-set-www-claim": "c1"}),
                   "1": Reply({"users": [{"username": "example_creator"}]})}

        def following(request):
            return answers[(request.params or {}).get("max_id")]

        for cookies, user_id in (({"sessionid": "s", "ds_user_id": "99"}, "99"),
                                 ({"sessionid": "77%3Aabc%3A2"}, "77")):
            with self.subTest(user_id=user_id):
                found, ctx = self.follows(cookies, {self.URL.format(user_id): following})
                self.assertEqual(found, [Target("ins:example_user"), Target("ins:example_creator")])
                [first, second] = ctx.seen.requests
                self.assertEqual((first[2]["params"], first[2]["in_page"]), ({"count": 100}, True))
                self.assertEqual(second[2]["headers"]["X-IG-WWW-Claim"], "c1")

    def test_no_login_or_an_odd_answer_raises(self):
        with self.assertRaises(LoginError):
            self.follows({"sessionid": "opaque"})
        with self.assertRaises(Changed):
            self.follows({"ds_user_id": "99"}, {self.URL.format("99"): Reply({"status": "ok"})})

    def test_me_caches_user_ids_so_api_posts_skips_web_profile_info(self):
        """`me()` caches `username -> user_id`, so `api_posts()` skips `web_profile_info`."""
        plugin = Instagram()
        routes = {
            self.URL.format("99"): Reply({"users": [
                {"username": "example_user", "pk": 4242},
                {"username": "example_creator", "id": "6074"},
            ]}),
            f"{SITE}/api/v1/feed/user/4242/": Reply({
                "items": [feed("P1", **image("https://cdn.example/p1.jpg"))],
                "more_available": False,
            }),
        }
        cookies = {"sessionid": "99%3As%3A1"}
        me_ctx = FakeCtx(plugin, "ins:me", cookies=cookies, routes=routes)
        self.assertEqual(listed(plugin.me(me_ctx, "me")),
                         [Target("ins:example_user"), Target("ins:example_creator")])
        self.assertEqual(plugin.user_ids, {"example_user": "4242", "example_creator": "6074"})
        user_ctx = FakeCtx(plugin, "ins:example_user", cookies=cookies, routes=routes)
        saved = collect(Instagram, "ins:example_user", ctx=user_ctx, categories=["posts"])
        self.assertEqual([item.path for item in saved], [f"posts/{DAY}_P1.jpg"])
        self.assertEqual(fetched(user_ctx), [f"{SITE}/api/v1/feed/user/4242/"])


class InPage:
    """The target's tab, as `ins:me`'s in-page calls reach it: answered by the case's `serve`."""

    url = SITE

    def __init__(self, case):
        self.case = case

    async def fetch(self, url, **options):
        reply = self.case.serve(_net.Request("GET", _net.URL(url), {}, None))
        return _net.Response(reply.status, url, {}, reply.body)

class MeEngineTest(fakes.EngineCase):
    """`ins:me` through the real engine: a target that finds the follows, recorded once."""

    def setUp(self):
        super().setUp()
        self.follows = [{"users": [{"username": "example_user"}]},
                        {"users": [{"username": "example_creator"},
                                   {"username": "example_user"}]}]
        self.routes[f"{SITE}/api/v1/friendships/99/following/"] = (
            lambda request: _net.reply(200, json=self.follows[0]))
        fakes.sign_in(self.home, "ins", [{"name": "sessionid", "value": "99:abc:1",
                                    "domain": ".instagram.com", "path": "/"}])

        async def read_tab(ctx, profile, tab):
            code = "A1" if profile.user == "example_user" else "B1"
            yield Post(code, [File(f"https://cdn.example/{code.lower()}.jpg")], date=day(STAMP))

        async def nothing(*args, **options):
            return []

        # The tabs and the pages a sync's every category reads, so no browser opens.
        self.enterContext(mock.patch("flnd.plugins.ins.read_tab", read_tab))
        self.enterContext(mock.patch("flnd.plugins.ins.story_page", nothing))
        self.enterContext(mock.patch("flnd.plugins.ins.page_results", nothing))
        self.enterContext(mock.patch("flnd.engine.ctx.Ctx._page", lambda ctx: InPage(self)))

    def saved(self, user, code):
        return (self.target_dir("ins", user) / "posts" / f"{DAY}_{code}.jpg").exists()

    def test_a_dry_run_keeps_nothing_and_a_sync_finds_new_follows(self):
        engine = fakes.Engine(home=self.home, sources={"ins": Instagram})
        dry = engine.download(["ins:me|posts"], dry=True)
        self.assertTrue(dry.ok, (dry, self.lines()))
        self.assertEqual(fakes.targets(self.home), {})
        report = engine.download(["ins:me|posts"], sync=True)
        self.assertTrue(report.ok, report)
        self.assertEqual([row.text for row in engine.tally.publish(force=True).targets],
                         ["ins:me", "ins:example_user"])
        rows = fakes.targets(self.home)
        self.assertEqual((list(rows), rows["ins:me"].seen), (["ins:me"], ["ins:example_user"]))
        [listed] = engine.target_ls()
        self.assertEqual((str(listed.target), listed.query, listed.seen), ("ins:me", True, 1))
        self.assertTrue(self.saved("example_user", "A1"))
        self.follows.pop(0)
        report = engine.sync()
        self.assertTrue(report.ok, report)
        self.assertEqual([row.text for row in engine.tally.publish(force=True).targets],
                         ["ins:me", "ins:example_creator"])
        self.assertEqual([str(found.target) for found in report.targets], ["ins:me"])
        self.assertEqual(list(fakes.targets(self.home)), ["ins:me"])
        self.assertTrue(self.saved("example_creator", "B1"))


if __name__ == "__main__":
    unittest.main()
