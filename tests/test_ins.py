"""Instagram: tabs and their replayed pagination, posts read through the media API (or their page), stories,
highlights, the picture, one post, and every throttle waited out.

What is faked: the site's answers (`FakeSite` through `collect`), and the tab's scrolling, by `FakeWatch`,
which hands out the GraphQL responses scrolling a tab would bring, each with the request a replay
would send again. No network, no browser, no wait is slept: `back_off` only records.
"""

import json
import os
import time
import unittest
from datetime import date
from types import SimpleNamespace
from typing import Any, NamedTuple
from unittest import mock
from urllib.parse import parse_qsl, urlencode

import httpx
from fakes import FakeSite, Seen, collect
from test_dash import INSTAGRAM

from flnd.session import Options
from flnd.source import FetchError, LoginError, Target, TargetError, day
from flnd.sources.common.relay import embedded_results
from flnd.sources.common.replay import Replay
from flnd.sources.ins import (APP_ID, Instagram, best_file, files_of, full_size, is_complete, pk_of_code, pk_stamp,
                              post_files, reel_posts, tab_media)
from shared import http, selector

SITE = "https://www.instagram.com"
API = f"{SITE}/api/v1"
GRAPHQL = f"{SITE}/graphql/query"
# 2023-11-15 12:00 UTC: the same day in every time zone, so a name doesn't depend on where the tests run.
STAMP = 1700049600
DAY = "2023-11-15"
# Instagram's id epoch, in milliseconds: an id holds its creation time above its lowest 23 bits.
EPOCH_MS = 1314220021721
THROTTLE = {"message": "Please wait a few minutes before you try again.", "status": "fail"}


def pk_at(stamp, n=1):
    """An Instagram id made at `stamp` (Unix seconds), the n-th of that millisecond."""
    return str(((stamp * 1000 - EPOCH_MS) << 23) | n)


def embedded(*results):
    """A page whose HTML embeds `results` as Instagram's server does."""
    blocks = "".join(f'<script type="application/json">{json.dumps({"require": [["x", {"__bbox": {"result": result}}]]})}'
                     "</script>" for result in results)
    return f"<html><body>{blocks}<script type='application/json'>{{'__bbox': not json</script></body></html>"


def image(url, width=640):
    return {"original_width": width,
            "image_versions2": {"candidates": [{"url": url.replace(".jpg", "_s.jpg"), "width": 150, "height": 150},
                                               {"url": url, "width": width, "height": width}]}}


def video(url):
    return {**image(url.replace(".mp4", ".jpg")), "media_type": 2,
            "video_versions": [{"url": url.replace(".mp4", "_low.mp4"), "width": 480, "height": 854},
                               {"url": url, "width": 720, "height": 1280}]}


def feed(code, **media):
    """A grid item that is a whole post: a plain feed photo, dated, at full size."""
    return {"code": code, "product_type": "feed", "taken_at": STAMP, **media}


def grid(*nodes, more=None):
    """A posts-tab result; `more` is the next page's cursor, "" for the last page, None for no `page_info`."""
    connection: dict[str, Any] = {"edges": [{"node": node} for node in nodes]}
    if more is not None:
        connection["page_info"] = {"has_next_page": bool(more), "end_cursor": more or None}
    return {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": connection}}


def reels_grid(*items):
    return {"data": {"fetch__XDTUserDict": {"clips_connection": {"edges": [
        {"node": {"media": {"code": code, **({"pk": pk} if pk else {}), "product_type": "clips",
                            **image(f"https://cdn.example/{code}_thumb.jpg")}}} for code, pk in items]}}}}


def info(pk, media):
    """The media API's answer for one post."""
    return {f"{API}/media/{pk}/info/": {"items": [media], "num_results": 1, "status": "ok"}}


PROFILE = {"data": {"user": {"id": "42", "hd_profile_pic_url_info": {"url": "https://cdn.example/v/123_456_n.jpg?x=1"}}}}
HIGHLIGHTS = {"data": {"highlights": {"edges": [{"node": {"id": "highlight:9001", "title": "Trip/2026"}}]}}}


# Every form ins reads, as people write it, and what it becomes: (kind, value), or a refusal's text.
# The kinds and values are the keys of targets.db and the history, so a change here is a migration.
ROUTES = [
    ("ins:example_user", ("user", "example_user")),
    ("ins:@example_user", ("user", "example_user")),
    ("ins:example.user", ("user", "example.user")),
    ("ins:instagram.com/example_user", ("user", "example_user")),
    ("https://www.instagram.com/example_user/", ("user", "example_user")),
    ("https://instagram.com/example_user/?hl=en", ("user", "example_user")),
    ("instagram.com/example_user", ("user", "example_user")),
    ("http://instagram.com/example_user#top", ("user", "example_user")),
    ("https://www.instagram.com/@example_user", ("user", "example_user")),
    # A profile's tab is the profile.
    ("https://m.instagram.com/example_user/reels/", ("user", "example_user")),
    ("https://www.instagram.com/example_user/tagged/", ("user", "example_user")),
    ("https://www.instagram.com/p/C1abcDEFghi/", ("post", "C1abcDEFghi")),
    ("https://www.instagram.com/p/C1a-bc_DEF/", ("post", "C1a-bc_DEF")),
    ("https://www.instagram.com/reel/C1abcDEFghi/?igsh=1", ("post", "C1abcDEFghi")),
    ("ins:https://www.instagram.com/reel/C1abcDEFghi/", ("post", "C1abcDEFghi")),
    ("https://www.instagram.com/reels/C1abcDEFghi/", ("post", "C1abcDEFghi")),
    ("https://www.instagram.com/tv/C1abcDEFghi/", ("post", "C1abcDEFghi")),
    ("instagram.com/example_user/p/C1abcDEFghi", ("post", "C1abcDEFghi")),
    ("https://www.instagram.com/example_user/reel/C1abcDEFghi/", ("post", "C1abcDEFghi")),
    ("https://www.instagram.com/example_user/reels/C1abcDEFghi/", ("post", "C1abcDEFghi")),
    ("https://www.instagram.com/example_user/tv/C1abcDEFghi/", ("post", "C1abcDEFghi")),
    ("https://www.instagram.com/stories/example_user/3141592653589793238/", "story links aren't supported"),
    ("https://www.instagram.com/stories/highlights/17900000000000000/", "story links aren't supported"),
    ("ins:not a name", "can't read"),
    ("ins:ex-ample", "can't read"),
    ("https://www.instagram.com/", "can't read"),
    ("ins:example.com/example_user", "can't read"),
    ("ins:https://example.com/example_user", "can't read"),
]


class RoutesTest(unittest.TestCase):
    def test_every_form(self):
        for text, expected in ROUTES:
            with self.subTest(text=text):
                value = text.removeprefix("ins:")
                if isinstance(expected, tuple):
                    self.assertEqual(Instagram().resolve(value), Target("ins", *expected))
                else:
                    with self.assertRaisesRegex(TargetError, f"^ins: {expected}"):
                        Instagram().resolve(value)


class MediaTest(unittest.TestCase):
    def test_each_carousel_item_keeps_its_own_kind_at_its_best_size(self):
        post = {"code": "ABC", "taken_at": STAMP, "carousel_media": [image("https://cdn.example/1.jpg"),
                                                                   video("https://cdn.example/2.mp4")]}
        self.assertEqual(post_files(post), [("https://cdn.example/1.jpg", "jpg", None),
                                            ("https://cdn.example/2.mp4", "mp4", None)])

    def test_a_dash_manifest_gives_its_best_video_and_audio(self):
        reel = {**video("https://cdn.example/r.mp4"), "video_dash_manifest": INSTAGRAM}
        self.assertEqual(best_file(reel), ("https://cdn.example/1080.mp4", "mp4", "https://cdn.example/a95.mp4"))
        # A manifest dash.best can't use falls back to the progressive file.
        self.assertEqual(best_file({**reel, "video_dash_manifest": "<MPD/>"}), ("https://cdn.example/r.mp4", "mp4", None))

    def test_only_a_whole_feed_post_is_complete(self):
        """ins.md §3: product_type "feed", dated, and every image within 10% of its original width."""
        self.assertTrue(is_complete(feed("P", **image("https://cdn.example/p.jpg"))))
        self.assertTrue(is_complete(feed("V", **video("https://cdn.example/v.mp4"))))
        # A reel, a carousel, or an item without a product_type goes through the API.
        for product_type in ("clips", "carousel_container", None):
            with self.subTest(product_type=product_type):
                self.assertFalse(is_complete({**feed("P", **image("https://cdn.example/p.jpg")),
                                              "product_type": product_type}))
        # No date, a thumbnail (more than 10% under the original width), or a video without its files.
        self.assertFalse(is_complete({**feed("P", **image("https://cdn.example/p.jpg")), "taken_at": None}))
        self.assertFalse(is_complete(feed("P", **{**image("https://cdn.example/p.jpg"), "original_width": 1080})))
        self.assertTrue(full_size({**image("https://cdn.example/p.jpg", width=980), "original_width": 1080}))
        self.assertFalse(full_size({**image("https://cdn.example/p.jpg"), "original_width": None}))
        self.assertFalse(is_complete(feed("V", media_type=2, **image("https://cdn.example/v.jpg"))))
        self.assertFalse(is_complete(reels_grid(("R1", None))["data"]["fetch__XDTUserDict"]["clips_connection"]
                                     ["edges"][0]["node"]["media"]))

    def test_reposts_and_tagged_connections(self):
        reposts = {"data": {"fetch__XDTUserDict": {"user_reposts_timeline": {"repost_grid_items": [
            {"media": {"code": "RP"}}]}}}}
        tagged = {"data": {"xdt_api__v1__usertags__user_id__feed_connection": {"edges": [{"node": {"code": "TG"}}]}}}
        self.assertEqual(tab_media(reposts, "reposts"), [{"code": "RP"}])
        self.assertEqual(tab_media(tagged, "tagged"), [{"code": "TG"}])
        self.assertEqual(tab_media({"data": {}}, "posts"), [])

    def test_embedded_results_are_found_in_the_page(self):
        page = selector.parse(embedded(PROFILE, HIGHLIGHTS))
        self.assertEqual(embedded_results(page), [PROFILE, HIGHLIGHTS])

    def test_a_file_is_named_here_only_when_the_formats_would_name_it_otherwise(self):
        post = {"code": "C", "taken_at": STAMP, "carousel_media": [image("https://cdn.example/a.jpg"),
                                                                 image("https://cdn.example/media/bare")]}
        self.assertEqual([file.name for file in files_of(post)], [None, f"{DAY}_C_1.jpg"])
        self.assertEqual([file.name for file in files_of(post, pinned=True)], [f"{DAY}_C_0.jpg", f"{DAY}_C_1.jpg"])
        # A post without a date is named by its code alone.
        self.assertEqual(files_of({"code": "C", **image("https://cdn.example/a.jpg")}, pinned=True)[0].name, "C.jpg")

    def test_an_id_holds_its_date(self):
        self.assertEqual(day(pk_stamp(pk_at(STAMP))), date(2023, 11, 15))
        self.assertEqual(day(pk_stamp("3141592653589793238")), date(2023, 7, 7))
        # An id too large for a real one gives no date (the engine refuses one after next year), and no id none.
        self.assertIsNone(day(pk_stamp(str(2 ** 90))))
        self.assertIsNone(pk_stamp(None))
        self.assertIsNone(pk_stamp("C1abc"))

    def test_a_code_spells_its_pk(self):
        self.assertEqual(pk_of_code("CuZLd-iMknW"), "3141592653589793238")
        # A private post's longer code, and anything outside base64url, isn't a pk.
        self.assertIsNone(pk_of_code("CuZLd-iMknWxyzxyzxyzxyzxyzxyzxyzxyzxyz"))
        self.assertIsNone(pk_of_code("bad code"))

    def test_prepare_adds_instagrams_web_headers(self):
        source = Instagram()
        source.api.claim = "hmac.claim"
        request = source.prepare(httpx.Request("GET", f"{API}/media/1/info/"), {"csrftoken": "tok"})
        self.assertEqual({name: request.headers[name] for name in
                          ("x-ig-app-id", "x-asbd-id", "x-ig-www-claim", "x-csrftoken", "x-requested-with", "referer")},
                         {"x-ig-app-id": APP_ID, "x-asbd-id": "129477", "x-ig-www-claim": "hmac.claim",
                          "x-csrftoken": "tok", "x-requested-with": "XMLHttpRequest", "referer": f"{SITE}/"})


def page_of(code, **media):
    return embedded({"data": {"xig_fetch_media": {"code": code, **media}}})


def shortcode_page(*posts):
    return embedded({"data": {"xdt_api__v1__media__shortcode__web_info": {"items": list(posts)}}})


def story_page(*stories):
    return embedded({"data": {"xdt_api__v1__feed__reels_media": {"reels_media": [{"items": list(stories)}]}}})


# The ids of the posts the grids list incompletely, all made on STAMP's day.
PK = {code: pk_at(STAMP, n) for n, code in enumerate(("CAR", "R1", "NOFILE", "R2", "T1"), start=1)}
# What scrolling each tab of `someone` fetches. Whole feed posts are saved as listed; a carousel, a
# reel, a post without files and the reels grid's thumbnails come from the media API by their pk;
# GONE and RP2 have no pk, so their pages are read.
GRIDS = {
    "posts": grid(
        feed("P1", **image("https://cdn.example/p1.jpg")),
        {"code": "CAR", "pk": PK["CAR"], "product_type": "carousel_container", "taken_at": STAMP,
         **image("https://cdn.example/c0.jpg")},
        {"code": "R1", "pk": PK["R1"], "product_type": "clips", "taken_at": STAMP, **image("https://cdn.example/r1.jpg")},
        feed("WEB", **image("https://cdn.example/w.webp")),
        feed("BARE", **image("https://cdn.example/media/bare")),
        {"code": "NOFILE", "pk": PK["NOFILE"], "product_type": "feed", "taken_at": STAMP}),
    "reels": reels_grid(("R1", PK["R1"]), ("R2", PK["R2"]), ("GONE", None)),
    "tagged": {"data": {"xdt_api__v1__usertags__user_id__feed_connection": {"edges": [
        {"node": {"code": "T1", "pk": PK["T1"]}}, {"node": feed("P1", **image("https://cdn.example/p1.jpg"))}]}}},
    "reposts": {"data": {"fetch__XDTUserDict": {"user_reposts_timeline": {"repost_grid_items": [
        {"media": feed("RP1", **image("https://cdn.example/rp1.jpg"))},
        {"media": {"code": "RP2"}}]}}}},
}
TABS = {"posts": "", "reels": "reels/", "tagged": "tagged/", "reposts": "reposts/"}
STORY_5001 = {"pk": "5001", "taken_at": STAMP, **image("https://cdn.example/s.jpg")}
# A story without `taken_at` isn't dated by its small pk (which would say 2011): it has no date.
STORY_5003 = {"pk": "5003_42", **video("https://cdn.example/s3.mp4")}
HIGHLIGHT_ITEMS = [{"id": "5002_42", "taken_at": STAMP, **video("https://cdn.example/h.mp4")},
                   {"id": "5004", "taken_at": STAMP, **image("https://cdn.example/h2.jpg")}]
REELS = {"42": [STORY_5001, STORY_5003], "highlight:9001": HIGHLIGHT_ITEMS}


def reels_media(url, request):
    """The reels API: each reel asked for (`reel_ids`, repeated) with its stories."""
    return {"reels_media": [{"id": reel, "items": REELS.get(reel, [])} for name, reel in request["params"]
                            if name == "reel_ids"], "status": "ok"}


JSON = {
    **info(PK["CAR"], {"code": "CAR", "taken_at": STAMP, "carousel_media": [
        image("https://cdn.example/c0.jpg"), {**video("https://cdn.example/c1.mp4"), "video_dash_manifest": INSTAGRAM}]}),
    **info(PK["R1"], {"code": "R1", "taken_at": STAMP, **video("https://cdn.example/r1.mp4")}),
    **info(PK["NOFILE"], {"code": "NOFILE", "taken_at": STAMP}),
    **info(PK["R2"], {"code": "R2", "taken_at": STAMP, **video("https://cdn.example/r2.mp4")}),
    **info(PK["T1"], {"code": "T1", "taken_at": STAMP, "carousel_media": [
        image("https://cdn.example/t0.jpg"), image("https://cdn.example/t1.jpg")]}),
    f"{API}/feed/reels_media/": reels_media,
    f"{API}/feed/user/42/story/": {"reel": {"id": "42", "items": REELS["42"]}, "status": "ok"},
}
PAGES = {
    f"{SITE}/someone/": embedded(PROFILE, HIGHLIGHTS),
    f"{SITE}/p/GONE/": "<html></html>",
    f"{SITE}/p/RP2/": page_of("RP2", taken_at=STAMP, **image("https://cdn.example/rp2.jpg")),
    f"{SITE}/stories/someone/": story_page(STORY_5001, STORY_5003),
    f"{SITE}/stories/highlights/9001/": embedded({"data": {"xdt_api__v1__feed__reels_media__connection": {"edges": [
        {"node": {"items": HIGHLIGHT_ITEMS}}]}}}),
    f"{SITE}/p/SINGLE/": shortcode_page({"code": "SINGLE", "taken_at": STAMP, "carousel_media": [
        image("https://cdn.example/a.jpg"), {**video("https://cdn.example/r.mp4"), "video_dash_manifest": INSTAGRAM}]}),
}


class Sent(NamedTuple):
    """A response scrolling brings, with the request the page sent for it."""

    data: Any
    request: Any


def pagination_request(after="c1", **headers):
    """The posts tab's next-page request as Playwright shows it: a form POST with its tokens."""
    body = urlencode({"doc_id": "7950326061742207", "fb_dtsg": "token",
                      "variables": json.dumps({"after": after, "before": None, "first": 12, "id": "42"})})
    return SimpleNamespace(url=GRAPHQL, method="POST", post_data=body,
                           headers={"content-type": "application/x-www-form-urlencoded", "x-fb-lsd": "lsd",
                                    "x-fb-friendly-name": "PolarisProfilePostsTabContentQuery_connection",
                                    "x-csrftoken": "tok", "user-agent": "Chrome", "cookie": "sessionid=s", **headers})


class FakeWatch:
    """`watch_responses` over a fake tab: scrolling gives the responses held for the URL last loaded.

    Each URL's responses are one queue, so a second watcher of the same tab (the scroll a refused
    replay falls back to) goes on where the first one stopped, as a real tab's scrolling would.
    """

    def __init__(self, batches):
        self.batches = {url: list(entries) for url, entries in batches.items()}
        self.opened = 0

    def __call__(self, site, match, *, scroll=False, lines=False):
        self.opened += 1
        batches = self.batches

        class Responses:
            request = None

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                pass

            def __iter__(self):
                # Read once the page is loaded, as the first scroll would.
                queue = batches.get(site.page.url, [])
                while queue:
                    entry = queue.pop(0)
                    self.request = entry.request if isinstance(entry, Sent) else None
                    yield entry.data if isinstance(entry, Sent) else entry

        return Responses()


def scrolled(*tabs):
    """The responses scrolling each of `tabs` of `someone` fetches."""
    return FakeWatch({f"{SITE}/someone/{TABS[tab]}": [GRIDS[tab]] for tab in tabs})


def read(text="ins:someone", *, options=Options(), watch=None, pages=PAGES, json=JSON, seen=None, cookies=None,
         state=None):
    """What the job would download for `text`, with `watch` as the tabs' scrolling (every tab's by default)."""
    with mock.patch("flnd.sources.ins.watch_responses", watch or scrolled(*TABS)):
        return collect(Instagram, text, pages=pages, json=json, options=options, seen=seen, cookies=cookies,
                       state=state)


def api_urls(seen):
    """The API calls a run made, without their query, in order."""
    return [url.split("?")[0] for _, url, _ in seen.requests]


# What ins named and keyed before routes, recorded with the pre-routes `run(session)` source in
# a UTC time zone over these fixtures (then read from each post's page, now from the media API for
# the posts with a pk). They are sorted: the old run opened every lazy post after the last tab, and
# the new one lists each tab's when the tab is done.
OLD_PROFILE = [
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
OLD_SINGLE = [("2023-11-15_SINGLE_0.jpg", "SINGLE_0"), ("2023-11-15_SINGLE_1.mp4", "SINGLE_1")]


class StoryMusicTest(unittest.TestCase):
    """fixes #11: a story's music is saved beside it, as a second file of its post."""

    def story(self, **extra):
        return {"pk": "31", "taken_at": 1767312000, "image_versions2": {"candidates": [
            {"url": "https://cdn.example/s.jpg", "width": 1080, "height": 1920}]}, **extra}

    def test_both_shapes_of_music_become_an_m4a_beside_the_story(self):
        sticker = {"story_music_stickers": [{"music_asset_info": {"progressive_download_url": "https://cdn.example/m1"}}]}
        metadata = {"music_metadata": {"music_info": {"music_asset_info": {
            "progressive_download_url": "https://cdn.example/m2"}}}}
        for extra, url in ((sticker, "https://cdn.example/m1"), (metadata, "https://cdn.example/m2")):
            [post] = reel_posts(FakeSite(Target("ins", "user", "someone")), [self.story(**extra)], "stories")
            self.assertEqual([(file.url, file.name) for file in post.files],
                             [("https://cdn.example/s.jpg", "2026-01-02_31.jpg"), (url, "2026-01-02_31_music.m4a")])

    def test_a_story_without_music_has_one_file(self):
        [post] = reel_posts(FakeSite(Target("ins", "user", "someone")), [self.story()], "stories")
        self.assertEqual(len(post.files), 1)


class NamesAndKeysTest(unittest.TestCase):
    def test_a_profile_is_named_and_keyed_as_before_routes(self):
        saved = read(options=Options(categories=("all",)))
        self.assertEqual(sorted((item.path, item.key) for item in saved), OLD_PROFILE)

    def test_the_story_pages_name_and_key_them_the_same(self):
        """The fallback: with the reels API refused, the story pages give the same paths and keys."""
        refused = {**JSON, f"{API}/feed/reels_media/": FetchError("HTTP 500", status=None)}
        saved = read(options=Options(categories=("stories", "highlights")), json=refused)
        self.assertEqual(sorted((item.path, item.key) for item in saved),
                         [pair for pair in OLD_PROFILE if pair[0].startswith(("stories/", "highlights/"))])

    def test_a_replayed_tab_is_named_and_keyed_as_a_scrolled_one(self):
        site = replay_site()
        self.assertEqual(sorted((item.path, item.key) for item in site.saved),
                         [(f"posts/{DAY}_P1.jpg", "P1_0"), (f"posts/{DAY}_P2.jpg", "P2_0"), (f"posts/{DAY}_P3.jpg", "P3_0")])

    def test_a_post_is_named_and_keyed_as_before_routes(self):
        saved = read("https://www.instagram.com/p/SINGLE/")
        self.assertEqual([(item.path, item.key) for item in saved], OLD_SINGLE)
        self.assertEqual((saved[1].file.url, saved[1].file.audio),
                         ("https://cdn.example/1080.mp4", "https://cdn.example/a95.mp4"))

    def test_a_post_from_the_media_api_is_named_and_keyed_as_from_its_page(self):
        code = "CuZLd-iMknW"
        media = {"code": code, "taken_at": STAMP, "carousel_media": [
            image("https://cdn.example/a.jpg"), {**video("https://cdn.example/r.mp4"), "video_dash_manifest": INSTAGRAM}]}
        seen = Seen()
        saved = read(f"https://www.instagram.com/p/{code}/", json=info("3141592653589793238", media), seen=seen)
        self.assertEqual([(item.path, item.key) for item in saved],
                         [(f"{DAY}_{code}_0.jpg", f"{code}_0"), (f"{DAY}_{code}_1.mp4", f"{code}_1")])
        self.assertEqual(seen.visited, [])

    def test_the_date_in_a_name_is_the_utc_day(self):
        # 23:00 UTC on 14 November is already the 15th in Auckland, where the old names took their day from.
        late = 1700002800
        pages = {f"{SITE}/p/LATE/": shortcode_page({"code": "LATE", "taken_at": late,
                                                    **image("https://cdn.example/l.jpg")})}
        with mock.patch.dict(os.environ, {"TZ": "Pacific/Auckland"}):
            time.tzset()
            saved = read("https://www.instagram.com/p/LATE/", pages=pages)
        time.tzset()
        self.assertEqual([(item.path, item.key) for item in saved], [("2023-11-14_LATE.jpg", "LATE_0")])


class ProfileTest(unittest.TestCase):
    def test_every_category_has_its_own_folder_and_headers(self):
        saved = read(options=Options(categories=("all",)))
        self.assertEqual({item.post.category for item in saved},
                         {"posts", "reels", "tagged", "reposts", "stories", "highlights", "profile"})
        by_path = {item.path: item for item in saved}
        self.assertEqual(by_path[f"posts/{DAY}_R1.mp4"].file.url, "https://cdn.example/r1.mp4")
        self.assertIsNone(by_path[f"posts/{DAY}_R1.mp4"].file.audio)
        self.assertEqual(by_path[f"posts/{DAY}_CAR_1.mp4"].file.audio, "https://cdn.example/a95.mp4")
        self.assertEqual(by_path[f"posts/{DAY}_P1.jpg"].post.headers, {"Referer": f"{SITE}/"})
        self.assertEqual(by_path["profile/123_456_n.jpg"].post.headers, {"Referer": f"{SITE}/"})

    def test_a_grid_post_without_details_is_lazy_and_dated_by_its_pk(self):
        site = FakeSite(Target("ins", "user", "someone"), pages=PAGES, json=JSON)
        instance = Instagram()
        with mock.patch("flnd.sources.ins.watch_responses", scrolled(*TABS)):
            listed = list(instance.reels(site, instance.user(site, "someone")))
        self.assertEqual([(post.id, post.lazy, day(post.date)) for post in listed],
                         [("R1", True, date(2023, 11, 15)), ("R2", True, date(2023, 11, 15)), ("GONE", True, None)])
        # Nothing is asked before the engine wants a post's files.
        self.assertEqual(site._seen.requests, [])

    def test_tagged_posts_come_from_the_media_api_and_a_post_without_a_pk_from_its_page(self):
        seen = Seen()
        read(options=Options(categories=("tagged", "reposts")), seen=seen)
        self.assertEqual(api_urls(seen), [f"{API}/media/{PK['T1']}/info/"])
        self.assertEqual(seen.visited, [f"{SITE}/someone/tagged/", f"{SITE}/someone/reposts/", f"{SITE}/p/RP2/"])

    def test_a_reel_the_posts_grid_lists_is_not_read_again(self):
        seen = Seen()
        read(options=Options(categories=("posts", "reels")), seen=seen)
        # R1 is on the posts grid already, so only R2 is asked for, and the gone post's page opened.
        self.assertEqual(api_urls(seen), [f"{API}/media/{PK[code]}/info/" for code in ("CAR", "R1", "NOFILE", "R2")])
        self.assertEqual(seen.visited, [f"{SITE}/someone/", f"{SITE}/someone/reels/", f"{SITE}/p/GONE/"])
        self.assertIn("! post GONE: its page holds no post; skipped", seen.messages)

    def test_a_lazy_post_outside_time_makes_no_request(self):
        seen = Seen()
        options = Options(categories=("reels",), since=date(2023, 11, 16))
        self.assertEqual(read(options=options, seen=seen), [])
        # R1 and R2 are dated by their pk and skipped before any request; GONE has no date, so its page is read.
        self.assertEqual(seen.requests, [])
        self.assertEqual(seen.visited, [f"{SITE}/someone/reels/", f"{SITE}/p/GONE/"])

    def test_the_profile_alone_loads_the_profile_page_once(self):
        seen = Seen()
        saved = read(options=Options(categories=("profile",)), seen=seen)
        self.assertEqual(seen.visited, [f"{SITE}/someone/"])
        self.assertEqual([(item.path, item.key) for item in saved], [("profile/123_456_n.jpg", "profile/123_456_n.jpg")])

    def test_stories_and_highlights_come_from_the_reels_api_with_one_load_of_the_profile(self):
        seen = Seen()
        read(options=Options(categories=("stories", "highlights", "profile")), seen=seen)
        self.assertEqual(seen.visited, [f"{SITE}/someone/"])
        self.assertEqual([url.removeprefix(API) for _, url, _ in seen.requests],
                         ["/feed/user/42/story/", "/feed/reels_media/"])
        self.assertEqual(seen.requests[1][2]["params"], [("reel_ids", "highlight:9001")])

    def test_a_profile_page_that_embeds_nothing_gives_its_user_and_highlights_by_its_own_calls(self):
        # 2026-11, live: the page's HTML holds neither the user nor the highlights; its GraphQL calls do.
        pages = {**PAGES, f"{SITE}/someone/": "<html></html>"}
        watch = FakeWatch({f"{SITE}/someone/": [PROFILE, HIGHLIGHTS]})
        seen = Seen()
        read(options=Options(categories=("stories", "highlights")), pages=pages, watch=watch, seen=seen)
        self.assertEqual(seen.visited, [f"{SITE}/someone/"])
        self.assertEqual([url.removeprefix(API) for _, url, _ in seen.requests],
                         ["/feed/user/42/story/", "/feed/reels_media/"])
        self.assertEqual(seen.requests[1][2]["params"], [("reel_ids", "highlight:9001")])

    def test_highlights_are_asked_for_five_at_a_time(self):
        nodes = [{"node": {"id": f"highlight:{n}", "title": f"H{n}"}} for n in range(7)]
        pages = {**PAGES, f"{SITE}/someone/": embedded(PROFILE, {"data": {"highlights": {"edges": nodes}}})}
        seen = Seen()
        read(options=Options(categories=("highlights",)), pages=pages, seen=seen)
        self.assertEqual([len(options["params"]) for _, _, options in seen.requests], [5, 2])

    def test_a_refused_story_api_falls_back_to_the_story_page(self):
        seen = Seen()
        refused = {**JSON, f"{API}/feed/user/42/story/": FetchError("HTTP 400", status=400)}
        saved = read(options=Options(categories=("stories",)), json=refused, seen=seen)
        self.assertEqual([item.path for item in saved], [f"stories/{DAY}_5001.jpg", "stories/5003.mp4"])
        self.assertEqual(seen.visited, [f"{SITE}/someone/", f"{SITE}/stories/someone/"])
        # flnd's client was refused first, then the tab's call: one warning each.
        self.assertIn("! Instagram refused flnd's own client; sending its API calls from the page", seen.messages)
        self.assertIn("stories: reading the story pages, as Instagram's story API is refusing", seen.messages)

    def test_an_answer_that_is_not_a_reel_is_a_refusal_and_a_missing_reel_is_no_stories(self):
        # 2026-11, live: the story endpoint can send the home page, which isn't JSON.
        for answer in ("<html>home</html>", {"status": "ok"}):
            with self.subTest(answer=answer):
                seen = Seen()
                saved = read(options=Options(categories=("stories",)), json={**JSON, f"{API}/feed/user/42/story/": answer},
                             seen=seen)
                self.assertEqual(len(saved), 2)
                self.assertEqual(seen.visited, [f"{SITE}/someone/", f"{SITE}/stories/someone/"])
                self.assertEqual(len([m for m in seen.messages if m.startswith("stories: reading the story pages")]), 1)
        seen = Seen()
        saved = read(options=Options(categories=("stories",)), seen=seen,
                     json={**JSON, f"{API}/feed/user/42/story/": {"reel": None, "status": "ok"}})
        self.assertEqual(saved, [])
        self.assertEqual(seen.visited, [f"{SITE}/someone/"])

    def test_a_refused_reels_api_is_warned_of_once_and_not_asked_again(self):
        nodes = [{"node": {"id": f"highlight:{n}", "title": f"H{n}"}} for n in range(7)]
        pages = {**PAGES, f"{SITE}/someone/": embedded(PROFILE, {"data": {"highlights": {"edges": nodes}}}),
                 **{f"{SITE}/stories/highlights/{n}/": "<html></html>" for n in range(7)}}
        seen = Seen()
        refused = {**JSON, f"{API}/feed/reels_media/": FetchError("HTTP 500", status=500)}
        read(options=Options(categories=("highlights",)), pages=pages, json=refused, seen=seen)
        # 7 highlights are two calls of 5 and 2; the second is never made.
        self.assertEqual(len([url for _, url, _ in seen.requests if url.endswith("/reels_media/")]), 1)
        self.assertEqual(seen.messages.count("highlights: reading the story pages, as Instagram's reels API is refusing"), 1)
        self.assertFalse([m for m in seen.messages if m.startswith("! highlights")])

    def test_a_refused_api_is_left_alone_for_a_day_then_asked_again(self):
        refused = {**JSON, f"{API}/feed/user/42/story/": FetchError("HTTP 500", status=500)}
        state: dict = {}
        stories = Options(categories=("stories",))

        def story_calls(now):
            seen = Seen()
            with mock.patch("flnd.sources.ins.time.time", return_value=now):
                saved = read(options=stories, json=refused, seen=seen, state=state)
            self.assertEqual(len(saved), 2)  # the story pages give the stories either way
            return len([url for _, url, _ in seen.requests if url.endswith("/story/")])

        start = 1_800_000_000.0
        self.assertEqual(story_calls(start), 1)
        self.assertEqual(story_calls(start + 60 * 60), 0)          # an hour later: not asked
        self.assertEqual(story_calls(start + 25 * 60 * 60), 1)     # a day later: asked again

    def test_a_throttled_response_backs_off_and_the_scroll_goes_on(self):
        seen = Seen()
        watch = FakeWatch({f"{SITE}/someone/": [THROTTLE, GRIDS["posts"]]})
        saved = read(options=Options(categories=("posts",)), watch=watch, seen=seen)
        self.assertEqual(seen.back_offs, ["Instagram asked to wait a few minutes"])
        self.assertIn(f"posts/{DAY}_P1.jpg", [item.path for item in saved])


def replay_site(*, answers=None, watch_tail=()):
    """The posts tab read by replay: page 1 embedded, page 2 scrolled with its request, page 3 by replay.

    Args:
        answers: what the replayed request gets, in order, before page 3; nothing more by default.
        watch_tail: responses scrolling brings after page 2, which only a fallback reads.

    Returns:
        A namespace with the `seen` record, the replayed requests and the `saved` files.
    """
    page_1 = grid(feed("P1", **image("https://cdn.example/p1.jpg")), more="c1")
    page_2 = grid(feed("P2", **image("https://cdn.example/p2.jpg")), more="c2")
    page_3 = grid(feed("P3", **image("https://cdn.example/p3.jpg")), more="")
    pending = list(answers or ())
    sent = []

    def graphql(url, request):
        form = dict(parse_qsl(request["data"]))
        sent.append(json.loads(form["variables"])["after"])
        answer = pending.pop(0) if pending else page_3
        return answer

    seen = Seen()
    watch = FakeWatch({f"{SITE}/someone/": [Sent(page_2, pagination_request()), *watch_tail]})
    saved = read(options=Options(categories=("posts",)), watch=watch, seen=seen,
                 pages={f"{SITE}/someone/": embedded(PROFILE, page_1)}, json={GRAPHQL: graphql})
    return SimpleNamespace(seen=seen, sent=sent, saved=saved, watch=watch)


class ReplayTest(unittest.TestCase):
    def test_the_tabs_own_request_is_replayed_with_the_next_cursor(self):
        site = replay_site()
        self.assertEqual(site.sent, ["c2"])
        method, url, options = site.seen.requests[0]
        self.assertEqual((method, url, options["in_page"]), ("POST", GRAPHQL, True))
        # The page's tokens go with it; what the browser sets itself doesn't.
        self.assertEqual(options["headers"], {"content-type": "application/x-www-form-urlencoded", "x-fb-lsd": "lsd",
                                              "x-fb-friendly-name": "PolarisProfilePostsTabContentQuery_connection",
                                              "x-csrftoken": "tok"})
        form = dict(parse_qsl(options["data"]))
        self.assertEqual((form["doc_id"], form["fb_dtsg"]), ("7950326061742207", "token"))
        self.assertEqual(json.loads(form["variables"]), {"after": "c2", "before": None, "first": 12, "id": "42"})

    def test_a_throttled_replay_waits_and_asks_the_same_page_again(self):
        site = replay_site(answers=[THROTTLE, FetchError("HTTP 400", status=400, response=http.Response(
            400, GRAPHQL, {"content-type": "application/json"}, json.dumps(THROTTLE).encode()))])
        self.assertEqual(site.sent, ["c2", "c2", "c2"])
        self.assertEqual(site.seen.back_offs, ["Instagram asked to wait a few minutes"] * 2)
        self.assertIn(f"posts/{DAY}_P3.jpg", [item.path for item in site.saved])

    def test_a_refused_replay_warns_once_and_scrolls_on(self):
        page_3 = grid(feed("P3", **image("https://cdn.example/p3.jpg")))
        site = replay_site(answers=[FetchError("HTTP 400", status=400)], watch_tail=[page_3])
        self.assertEqual(site.sent, ["c2"])
        self.assertEqual([message for message in site.seen.messages if message.startswith("!")],
                         ["! posts: the page's API refused a replay (HTTP 400); scrolling instead"])
        self.assertEqual(site.watch.opened, 2)
        self.assertIn(f"posts/{DAY}_P3.jpg", [item.path for item in site.saved])

    def test_an_answer_without_the_tab_is_a_refusal(self):
        site = replay_site(answers=[{"data": {"something_else": {}}}])
        self.assertTrue(any("refused a replay" in message for message in site.seen.messages))

    def test_a_tab_whose_embedded_page_is_its_last_is_not_scrolled(self):
        watch = FakeWatch({})
        pages = {f"{SITE}/someone/": embedded(PROFILE, grid(feed("P1", **image("https://cdn.example/p1.jpg")), more=""))}
        saved = read(options=Options(categories=("posts",)), watch=watch, pages=pages)
        self.assertEqual([item.path for item in saved], [f"posts/{DAY}_P1.jpg"])
        self.assertEqual(watch.batches, {})

    def test_a_request_without_the_cursor_is_not_replayed(self):
        request = SimpleNamespace(url=GRAPHQL, method="POST", post_data=urlencode({"variables": json.dumps({"id": 1})}),
                                  headers={})
        self.assertIsNone(Replay.of(request, cursor="after"))
        self.assertIsNone(Replay.of(None, cursor="after"))
        get = SimpleNamespace(url=f"{GRAPHQL}?variables=%7B%22after%22%3A%22c%22%7D&doc_id=1", method="GET",
                              post_data=None, headers={"X-IG-App-ID": "1"})
        replay = Replay.of(get, cursor="after")
        self.assertIsNotNone(replay)
        site = FakeSite(Target("ins", "user", "someone"), json={GRAPHQL: {"ok": True}})
        assert replay is not None
        replay.send(site, "next")
        _, url, options = site._seen.requests[0]
        query = dict(parse_qsl(url.split("?", 1)[1]))
        self.assertEqual((json.loads(query["variables"]), query["doc_id"]), ({"after": "next"}, "1"))
        self.assertEqual(options["headers"], {"X-IG-App-ID": "1"})


class Landing(FakeSite):
    """A `FakeSite` whose tab lands elsewhere on a URL's first visits: `lands={url: [landing, …]}`."""

    def __init__(self, target, *, lands, **options):
        super().__init__(target, **options)
        self.lands = lands

    @property
    def page(self):
        page = super().page
        url = self._seen.visited[-1] if self._seen.visited else "about:blank"
        visits = self._seen.visited.count(url)
        landings = self.lands.get(url, [])
        page.raw.url = landings[visits - 1] if visits <= len(landings) else url
        return page


class ThrottleTest(unittest.TestCase):
    def test_a_please_wait_answer_is_waited_out_and_the_same_post_saved(self):
        answers = [FetchError("HTTP 400", status=400, response=http.Response(
            400, f"{API}/media/{PK['T1']}/info/", {"content-type": "application/json"},
            json.dumps({**THROTTLE, "require_login": True}).encode())), THROTTLE]
        calls = []

        def media(url, request):
            calls.append(url)
            return answers.pop(0) if answers else JSON[f"{API}/media/{PK['T1']}/info/"]

        seen = Seen()
        saved = read(options=Options(categories=("tagged",)), json={**JSON, f"{API}/media/{PK['T1']}/info/": media},
                     seen=seen)
        self.assertEqual(len(calls), 3)
        self.assertEqual(seen.back_offs, ["Instagram asked to wait a few minutes"] * 2)
        self.assertEqual([item.path for item in saved][:2], [f"tagged/{DAY}_T1_0.jpg", f"tagged/{DAY}_T1_1.jpg"])

    def test_a_refusal_of_flnds_client_moves_the_calls_into_the_page(self):
        seen = Seen()

        def media(url, request):
            # flnd's own client is refused; the tab's fetch is answered.
            if not seen.requests[-1][2]["in_page"]:
                return FetchError("HTTP 403", status=403)
            return JSON[f"{API}/media/{PK['T1']}/info/"]

        saved = read(options=Options(categories=("tagged",)), json={**JSON, f"{API}/media/{PK['T1']}/info/": media},
                     seen=seen, cookies={"csrftoken": "tok", "sessionid": "s"})
        self.assertEqual([options["in_page"] for _, _, options in seen.requests], [False, True])
        self.assertEqual(seen.requests[1][2]["headers"]["X-CSRFToken"], "tok")
        self.assertEqual(seen.requests[1][2]["headers"]["X-IG-App-ID"], APP_ID)
        self.assertEqual(seen.back_offs, [])
        self.assertIn(f"tagged/{DAY}_T1_0.jpg", [item.path for item in saved])

    def test_a_refused_media_call_reads_the_posts_page(self):
        seen = Seen()
        pages = {**PAGES, f"{SITE}/p/T1/": shortcode_page({"code": "T1", "taken_at": STAMP,
                                                           **image("https://cdn.example/t.jpg")})}
        refused = {**JSON, f"{API}/media/{PK['T1']}/info/": FetchError("HTTP 500", status=None)}
        saved = read(options=Options(categories=("tagged",)), json=refused, pages=pages, seen=seen)
        self.assertIn(f"tagged/{DAY}_T1.jpg", [item.path for item in saved])
        self.assertIn(f"{SITE}/p/T1/", seen.visited)

    def test_a_login_wall_is_waited_out_and_the_page_loaded_again(self):
        site = Landing(Target("ins", "post", "SINGLE"), pages=PAGES,
                       lands={f"{SITE}/p/SINGLE/": [f"{SITE}/accounts/login/?next=/p/SINGLE/", f"{SITE}/challenge/"]})
        posts = list(Instagram().post(site, "SINGLE"))
        self.assertEqual([post.id for post in posts], ["SINGLE"])
        self.assertEqual(site._seen.visited, [f"{SITE}/p/SINGLE/"] * 3)
        self.assertEqual(site._seen.back_offs, ["Instagram showed a login wall"] * 2)

    def test_a_suspended_account_is_a_hard_stop(self):
        site = Landing(Target("ins", "post", "SINGLE"), pages=PAGES,
                       lands={f"{SITE}/p/SINGLE/": [f"{SITE}/accounts/suspended/?next=/p/SINGLE/"]})
        with self.assertRaisesRegex(LoginError, "suspended or locked"):
            list(Instagram().post(site, "SINGLE"))
        suspended = FetchError("HTTP 403", status=403, response=http.Response(
            403, f"{SITE}/accounts/suspended/", {"content-type": "text/html"}, b"<html></html>"))
        with self.assertRaisesRegex(LoginError, "suspended or locked"):
            read(options=Options(categories=("tagged",)), json={**JSON, f"{API}/media/{PK['T1']}/info/": suspended})

    def test_the_www_claim_is_kept_from_each_answer(self):
        source = Instagram()

        class Claiming(FakeSite):
            def fetch(self, url, **options):
                answer = super().fetch(url, **options)
                assert answer is not None
                return http.Response(200, url, {"x-ig-set-www-claim": "hmac.new"}, answer.content)

        site = Claiming(Target("ins", "user", "someone"), json=JSON)
        source.api.media(site, PK["R1"])
        self.assertEqual(source.api.claim, "hmac.new")


class SinglePostTest(unittest.TestCase):
    def test_a_carousel_post_from_its_page_has_no_category(self):
        saved = read("https://www.instagram.com/p/SINGLE/")
        self.assertEqual([item.path for item in saved], [f"{DAY}_SINGLE_0.jpg", f"{DAY}_SINGLE_1.mp4"])
        self.assertEqual([item.post.category for item in saved], ["", ""])

    def test_a_page_without_a_post_is_a_warning(self):
        seen = Seen()
        self.assertEqual(read("https://www.instagram.com/p/GONE/", seen=seen), [])
        self.assertIn("! post GONE: its page holds no post; skipped", seen.messages)

    def test_a_post_outside_time_is_dropped(self):
        self.assertEqual(read("https://www.instagram.com/p/SINGLE/", options=Options(until=date(2023, 11, 14))), [])

    def test_a_pk_that_gives_another_post_is_not_taken_for_it(self):
        seen = Seen()
        other = info(pk_of_code("SINGLE"), {"code": "OTHER", "taken_at": STAMP, **image("https://cdn.example/o.jpg")})
        saved = read("https://www.instagram.com/p/SINGLE/", json=other, seen=seen)
        self.assertEqual([(item.path, item.key) for item in saved], OLD_SINGLE)
        self.assertEqual(seen.visited, [f"{SITE}/p/SINGLE/"])


if __name__ == "__main__":
    unittest.main()
