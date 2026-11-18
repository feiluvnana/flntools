"""Instagram: tabs scrolled to the end, posts read through the media API (or their page), stories,
highlights, the picture, one post, and every throttle waited out.

What is faked: the site's answers (`FakeSite` through `collect`), and the tab's scrolling, by `FakeWatch`,
which hands out the GraphQL responses scrolling a tab would bring. No network, no browser, no wait
is slept: `back_off` only records.
"""

import json
import os
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from typing import Any
from unittest import mock

import fakes
from fakes import FakeSite, Seen, collect

from flnd.engine.core import Engine
from flnd.engine.options import Options
from flnd.source import Changed, LoginError, Request, SiteError, Target, UsageError
from flnd.sources.ins import (
    APP_ID,
    Instagram,
    best_file,
    embedded_results,
    files_of,
    full_size,
    is_complete,
    pk_of_code,
    pk_stamp,
    post_files,
    reel_posts,
    tab_media,
)
from flnkit import html as _html
from flnkit import net as _net
from flnkit.clock import day

# A DASH manifest as Meta serves it, for the reels' video and audio.
MANIFEST_HEAD = '<?xml version="1.0"?>\n<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period>'


def representation(mime, url, *, height=None, bandwidth=1000):
    size = f' height="{height}"' if height else ""
    return (f'<Representation mimeType="{mime}"{size} bandwidth="{bandwidth}">'
            f"<BaseURL>{url}</BaseURL></Representation>")


def manifest(*videos, audios=()):
    sets = f'<AdaptationSet>{"".join(videos)}</AdaptationSet>'
    if audios:
        sets += f'<AdaptationSet>{"".join(audios)}</AdaptationSet>'
    return MANIFEST_HEAD + sets + "</Period></MPD>"


# Meta serves each quality as one representation; some list the same height twice, told apart only
# by bandwidth.
INSTAGRAM = manifest(
    representation("video/mp4", "https://cdn.example/360.mp4", height=640, bandwidth=168170),
    representation("video/mp4", "https://cdn.example/1080.mp4", height=1920, bandwidth=1160975),
    representation("video/mp4", "https://cdn.example/720.mp4", height=1280, bandwidth=825291),
    audios=[representation("audio/mp4", "https://cdn.example/a64.mp4", bandwidth=64353),
            representation("audio/mp4", "https://cdn.example/a95.mp4", bandwidth=95480)])

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
    ("ins:me", ("me", "me")),
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
    ("https://www.instagram.com/explore/", "that's not a profile"),
    ("https://www.instagram.com/accounts/login/", "that's not a profile"),
    ("https://www.instagram.com/direct/inbox/", "that's not a profile"),
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
                    kind, found = expected
                    self.assertEqual(Instagram().resolve(value), Target(f"ins:{found}", kind=kind))
                else:
                    with self.assertRaisesRegex(UsageError, f"^ins: {expected}"):
                        Instagram().resolve(value)


class MediaTest(unittest.TestCase):
    def test_each_carousel_item_keeps_its_own_kind_at_its_best_size(self):
        post = {"code": "ABC", "taken_at": STAMP, "carousel_media": [image("https://cdn.example/1.jpg"),
                                                                   video("https://cdn.example/2.mp4")]}
        self.assertEqual(post_files(post), [("https://cdn.example/1.jpg", "jpg", None),
                                            ("https://cdn.example/2.mp4", "mp4", None)])

    def test_a_dash_manifest_gives_its_best_video_and_audio(self):
        reel = {**video("https://cdn.example/r.mp4"), "video_dash_manifest": INSTAGRAM}
        expected = (
            ("https://cdn.example/1080.mp4", "https://cdn.example/r.mp4"),
            "mp4",
            "https://cdn.example/a95.mp4",
        )
        self.assertEqual(best_file(reel), expected)
        # Without progressive video versions, only the DASH stream URL is returned.
        no_prog = {"video_dash_manifest": INSTAGRAM}
        expected_no_prog = ("https://cdn.example/1080.mp4", "mp4", "https://cdn.example/a95.mp4")
        self.assertEqual(best_file(no_prog), expected_no_prog)
        # A manifest dash.best can't use falls back to the progressive file.
        bad = {**reel, "video_dash_manifest": "<MPD/>"}
        self.assertEqual(best_file(bad), ("https://cdn.example/r.mp4", "mp4", None))
        # A segmented manifest also falls back to the progressive file.
        seg = {
            **video("https://cdn.example/r.mp4"),
            "video_dash_manifest": INSTAGRAM.replace(
                "</AdaptationSet>", '<SegmentTemplate media="seg-$Number$.m4s"/></AdaptationSet>', 1
            ),
        }
        self.assertEqual(best_file(seg), ("https://cdn.example/r.mp4", "mp4", None))

    def test_only_a_whole_feed_post_is_complete(self):
        """ins.md §3: product_type "feed", dated, and every image within 10% of its original width."""
        self.assertTrue(is_complete(feed("P", **image("https://cdn.example/p.jpg"))))
        self.assertTrue(is_complete(feed("V", **video("https://cdn.example/v.mp4"))))
        # A reel, a carousel, or an item without a product_type goes through the API.
        for product_type in ("clips", "carousel_container", None):
            with self.subTest(product_type=product_type):
                self.assertFalse(is_complete({**feed("P", **image("https://cdn.example/p.jpg")),
                                              "product_type": product_type}))
        # No date, a thumbnail (more than 10% under the original width), or a video without its paths.
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
        page = _html.parse(embedded(PROFILE, HIGHLIGHTS))
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

    def test_sign_adds_instagrams_web_headers(self):
        source = Instagram()
        source.api.claim = "hmac.claim"
        request = Request("GET", f"{API}/media/1/info/", {}, None, {"csrftoken": "tok"})
        source.sign(request)
        self.assertEqual(request.headers, {"X-IG-App-ID": APP_ID, "X-ASBD-ID": "129477", "X-IG-WWW-Claim": "hmac.claim",
                                           "X-CSRFToken": "tok", "X-Requested-With": "XMLHttpRequest",
                                           "Referer": f"{SITE}/"})


def page_of(code, **media):
    return embedded({"data": {"xig_fetch_media": {"code": code, **media}}})


def shortcode_page(*posts):
    return embedded({"data": {"xdt_api__v1__media__shortcode__web_info": {"items": list(posts)}}})


def story_page(*stories):
    return embedded({"data": {"xdt_api__v1__feed__reels_media": {"reels_media": [{"items": list(stories)}]}}})


# The ids of the posts the grids list incompletely, all made on STAMP's day.
PK = {code: pk_at(STAMP, n) for n, code in enumerate(("CAR", "R1", "NOFILE", "R2", "T1"), start=1)}
# What scrolling each tab of `example_creator` fetches. Whole feed posts are saved as listed; a carousel, a
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
JSON = {
    **info(PK["CAR"], {"code": "CAR", "taken_at": STAMP, "carousel_media": [
        image("https://cdn.example/c0.jpg"), {**video("https://cdn.example/c1.mp4"), "video_dash_manifest": INSTAGRAM}]}),
    **info(PK["R1"], {"code": "R1", "taken_at": STAMP, **video("https://cdn.example/r1.mp4")}),
    **info(PK["NOFILE"], {"code": "NOFILE", "taken_at": STAMP}),
    **info(PK["R2"], {"code": "R2", "taken_at": STAMP, **video("https://cdn.example/r2.mp4")}),
    **info(PK["T1"], {"code": "T1", "taken_at": STAMP, "carousel_media": [
        image("https://cdn.example/t0.jpg"), image("https://cdn.example/t1.jpg")]}),
}
PAGES = {
    f"{SITE}/example_creator/": embedded(PROFILE, HIGHLIGHTS),
    f"{SITE}/p/GONE/": "<html></html>",
    f"{SITE}/p/RP2/": page_of("RP2", taken_at=STAMP, **image("https://cdn.example/rp2.jpg")),
    f"{SITE}/stories/example_creator/": story_page(STORY_5001, STORY_5003),
    f"{SITE}/stories/highlights/9001/": embedded({"data": {"xdt_api__v1__feed__reels_media__connection": {"edges": [
        {"node": {"items": HIGHLIGHT_ITEMS}}]}}}),
    f"{SITE}/p/SINGLE/": shortcode_page({"code": "SINGLE", "taken_at": STAMP, "carousel_media": [
        image("https://cdn.example/a.jpg"), {**video("https://cdn.example/r.mp4"), "video_dash_manifest": INSTAGRAM}]}),
}


class FakeWatch:
    """`site.feed` over a fake tab: scrolling gives the responses held for the URL last loaded."""

    def __init__(self, batches):
        self.batches = {url: list(entries) for url, entries in batches.items()}
        self.opened = 0

    def __call__(self, site, match, *, scroll=False, lines=False, cursor=None):
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
                queue = batches.get(site.url, [])
                while queue:
                    yield queue.pop(0)

        return Responses()


def watching(watch):
    """Patch `FakeSite.feed` to be `watch`, a `FakeWatch`."""
    return mock.patch.object(FakeSite, "feed", lambda site, match, **options: watch(site, match, **options))


def scrolled(*tabs):
    """The responses scrolling each of `tabs` of `example_creator` fetches."""
    return FakeWatch({f"{SITE}/example_creator/{TABS[tab]}": [GRIDS[tab]] for tab in tabs})


def read(text="ins:example_creator", *, options=Options(), watch=None, pages=PAGES, json=JSON, seen=None, cookies=None,
         state=None):
    """What the job would download for `text`, with `watch` as the tabs' scrolling (every tab's by default)."""
    with watching(watch or scrolled(*TABS)):
        return collect(Instagram, text, pages=pages, json=json, options=options, seen=seen, cookies=cookies,
                       state=state)


def api_urls(seen):
    """The API calls a run made, without their query, in order."""
    return [url.split("?")[0] for _, url, _ in seen.requests]


# What ins named and keyed before routes, recorded with the pre-routes `run(session)` source in
# a UTC time zone over these fixtures (then read from each post's page, now from the media API for
# the posts with a pk). They are sorted: the old run opened every lazy post after the last tab, and
# the new one reads each where its tab lists it.
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
            [post] = reel_posts(FakeSite(Target("ins:example_creator", kind="user")), [self.story(**extra)], "stories")
            files = list(post.files() if callable(post.files) else post.files)
            self.assertEqual([(file.url, file.name) for file in files],
                             [("https://cdn.example/s.jpg", "2026-01-02_31.jpg"), (url, "2026-01-02_31_music.m4a")])

    def test_a_story_without_music_has_one_file(self):
        [post] = reel_posts(FakeSite(Target("ins:example_creator", kind="user")), [self.story()], "stories")
        files = list(post.files() if callable(post.files) else post.files)
        self.assertEqual(len(files), 1)


class NamesAndKeysTest(unittest.TestCase):
    def test_a_profile_is_named_and_keyed_as_before_routes(self):
        saved = read(options=Options(categories=("all",)))
        self.assertEqual(sorted((item.path, item.key) for item in saved), OLD_PROFILE)

    def test_the_story_pages_name_and_key_them_the_same(self):
        """The story pages give the expected paths and keys."""
        saved = read(options=Options(categories=("stories", "highlights")))
        self.assertEqual(sorted((item.path, item.key) for item in saved),
                         [pair for pair in OLD_PROFILE if pair[0].startswith(("stories/", "highlights/"))])

    def test_a_post_is_named_and_keyed_as_before_routes(self):
        saved = read("https://www.instagram.com/p/SINGLE/")
        self.assertEqual([(item.path, item.key) for item in saved], OLD_SINGLE)
        expected_url = ("https://cdn.example/1080.mp4", "https://cdn.example/r.mp4")
        self.assertEqual(
            (saved[1].file.url, saved[1].file.audio),
            (expected_url, "https://cdn.example/a95.mp4"),
        )

    def test_a_post_from_the_media_api_is_named_and_keyed_as_from_its_page(self):
        code = "CuZLd-iMknW"
        media = {"code": code, "taken_at": STAMP, "carousel_media": [
            image("https://cdn.example/a.jpg"), {**video("https://cdn.example/r.mp4"), "video_dash_manifest": INSTAGRAM}]}
        seen = Seen()
        saved = read(f"https://www.instagram.com/p/{code}/", json=info("3141592653589793238", media), seen=seen)
        self.assertEqual([(item.path, item.key) for item in saved],
                         [(f"{DAY}_{code}_0.jpg", f"{code}_0"), (f"{DAY}_{code}_1.mp4", f"{code}_1")])
        self.assertEqual(seen.visited, [])

    @unittest.skipUnless(hasattr(time, "tzset"), "no time.tzset on this platform")
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
        site = FakeSite(Target("ins:example_creator", kind="user"), pages=PAGES, json=JSON)
        instance = Instagram()
        with watching(scrolled(*TABS)):
            listed = list(instance.reels(site, instance.user(site, "example_creator")))
        self.assertEqual([(post.id, post.lazy, day(post.date)) for post in listed],
                         [("R1", True, date(2023, 11, 15)), ("R2", True, date(2023, 11, 15)), ("GONE", True, None)])
        # Nothing is asked before the engine wants a post's paths.
        self.assertEqual(site._seen.requests, [])

    def test_tagged_posts_come_from_the_media_api_and_a_post_without_a_pk_from_its_page(self):
        seen = Seen()
        read(options=Options(categories=("tagged", "reposts")), seen=seen)
        self.assertEqual(api_urls(seen), [f"{API}/media/{PK['T1']}/info/"])
        self.assertEqual(seen.visited, [f"{SITE}/example_creator/tagged/", f"{SITE}/example_creator/reposts/", f"{SITE}/p/RP2/"])

    def test_a_reel_the_posts_grid_lists_is_not_read_again(self):
        seen = Seen()
        read(options=Options(categories=("posts", "reels")), seen=seen)
        # R1 is on the posts grid already, so only R2 is asked for, and the gone post's page opened.
        self.assertEqual(api_urls(seen), [f"{API}/media/{PK[code]}/info/" for code in ("CAR", "R1", "NOFILE", "R2")])
        self.assertEqual(seen.visited, [f"{SITE}/example_creator/", f"{SITE}/example_creator/reels/", f"{SITE}/p/GONE/"])
        self.assertIn("! post GONE: its page holds no post; skipped", seen.messages)

    def test_a_lazy_post_outside_time_makes_no_request(self):
        seen = Seen()
        options = Options(categories=("reels",), since=date(2023, 11, 16))
        self.assertEqual(read(options=options, seen=seen), [])
        # R1 and R2 are dated by their pk and skipped before any request; GONE has no date, so its page is read.
        self.assertEqual(seen.requests, [])
        self.assertEqual(seen.visited, [f"{SITE}/example_creator/reels/", f"{SITE}/p/GONE/"])

    def test_the_profile_alone_loads_the_profile_page_once(self):
        seen = Seen()
        saved = read(options=Options(categories=("profile",)), seen=seen)
        self.assertEqual(seen.visited, [f"{SITE}/example_creator/"])
        self.assertEqual([(item.path, item.key) for item in saved], [("profile/123_456_n.jpg", "profile/123_456_n.jpg")])

    def test_stories_and_highlights_come_from_their_pages_directly(self):
        seen = Seen()
        read(options=Options(categories=("stories", "highlights", "profile")), seen=seen)
        self.assertEqual(seen.visited, [
            f"{SITE}/example_creator/",
            f"{SITE}/stories/example_creator/",
            f"{SITE}/stories/highlights/9001/",
        ])
        self.assertEqual(seen.requests, [])

    def test_a_profile_page_that_embeds_nothing_gives_its_user_and_highlights_by_its_own_calls(self):
        # 2026-11, live: the page's HTML holds neither the user nor the highlights; its GraphQL calls do.
        pages = {**PAGES, f"{SITE}/example_creator/": "<html></html>"}
        watch = FakeWatch({f"{SITE}/example_creator/": [PROFILE, HIGHLIGHTS]})
        seen = Seen()
        read(options=Options(categories=("stories", "highlights")), pages=pages, watch=watch, seen=seen)
        self.assertEqual(seen.visited, [
            f"{SITE}/example_creator/",
            f"{SITE}/stories/example_creator/",
            f"{SITE}/stories/highlights/9001/",
        ])
        self.assertEqual(seen.requests, [])

    def test_highlights_load_their_story_pages_directly(self):
        nodes = [{"node": {"id": f"highlight:{n}", "title": f"H{n}"}} for n in range(3)]
        profile = embedded(PROFILE, {"data": {"highlights": {"edges": nodes}}})
        pages = {
            **PAGES,
            f"{SITE}/example_creator/": profile,
            **{f"{SITE}/stories/highlights/{n}/": "<html></html>" for n in range(3)},
        }
        seen = Seen()
        read(options=Options(categories=("highlights",)), pages=pages, seen=seen)
        self.assertEqual(seen.visited, [
            f"{SITE}/example_creator/",
            f"{SITE}/stories/highlights/0/",
            f"{SITE}/stories/highlights/1/",
            f"{SITE}/stories/highlights/2/",
        ])
        self.assertEqual(seen.requests, [])

    def test_an_empty_story_page_yields_no_stories(self):
        pages = {**PAGES, f"{SITE}/stories/example_creator/": "<html></html>"}
        seen = Seen()
        saved = read(options=Options(categories=("stories",)), pages=pages, seen=seen)
        self.assertEqual(saved, [])
        self.assertEqual(seen.visited, [f"{SITE}/example_creator/", f"{SITE}/stories/example_creator/"])

    def test_a_story_page_without_reel_items_yields_no_stories(self):
        pages = {**PAGES, f"{SITE}/stories/example_creator/": embedded({"data": {}})}
        seen = Seen()
        saved = read(options=Options(categories=("stories",)), pages=pages, seen=seen)
        self.assertEqual(saved, [])
        self.assertEqual(seen.visited, [f"{SITE}/example_creator/", f"{SITE}/stories/example_creator/"])

    def test_a_throttled_response_backs_off_and_the_scroll_goes_on(self):
        seen = Seen()
        watch = FakeWatch({f"{SITE}/example_creator/": [THROTTLE, GRIDS["posts"]]})
        saved = read(options=Options(categories=("posts",)), watch=watch, seen=seen)
        self.assertEqual(seen.back_offs, ["Instagram asked to wait a few minutes"])
        self.assertIn(f"posts/{DAY}_P1.jpg", [item.path for item in saved])


class ScrollTest(unittest.TestCase):
    def test_a_lazy_post_is_read_where_the_tab_lists_it(self):
        """The engine asks for a lazy post's files as it is listed, so a sync can stop the scroll early."""
        page_1 = grid({"code": "CAR", "pk": PK["CAR"], "product_type": "carousel_container", "taken_at": STAMP},
                      more="c1")
        page_2 = grid(feed("P2", **image("https://cdn.example/p2.jpg")), more="c2")
        page_3 = grid({"code": "R1", "pk": PK["R1"], "product_type": "clips", "taken_at": STAMP}, more="")
        watch = FakeWatch({f"{SITE}/example_creator/": [page_2, page_3]})
        scroll_left_when_api_called = []

        def car_info(url, request):
            scroll_left_when_api_called.append(len(watch.batches[f"{SITE}/example_creator/"]))
            return JSON[f"{API}/media/{PK['CAR']}/info/"]

        seen = Seen()
        saved = read(options=Options(categories=("posts",)), watch=watch, seen=seen,
                     pages={f"{SITE}/example_creator/": embedded(PROFILE, page_1)},
                     json={**JSON, f"{API}/media/{PK['CAR']}/info/": car_info})
        # CAR is on the embedded first page: its files are asked for before any scroll.
        self.assertEqual(scroll_left_when_api_called, [2])
        self.assertEqual(sorted(item.path for item in saved), [
            f"posts/{DAY}_CAR_0.jpg", f"posts/{DAY}_CAR_1.mp4", f"posts/{DAY}_P2.jpg", f"posts/{DAY}_R1.mp4",
        ])

    def test_a_tab_whose_embedded_page_is_its_last_is_not_scrolled(self):
        watch = FakeWatch({})
        pages = {f"{SITE}/example_creator/": embedded(PROFILE, grid(feed("P1", **image("https://cdn.example/p1.jpg")), more=""))}
        saved = read(options=Options(categories=("posts",)), watch=watch, pages=pages)
        self.assertEqual([item.path for item in saved], [f"posts/{DAY}_P1.jpg"])
        self.assertEqual(watch.batches, {})

    def test_the_engine_stops_the_scroll_after_five_posts_in_a_row_older_than_when(self):
        old_stamp = STAMP - 10 * 86400
        old_posts = [feed(f"OLD{i}", taken_at=old_stamp, **image(f"https://cdn.example/o{i}.jpg")) for i in range(5)]
        page_1 = grid(feed("NEW", taken_at=STAMP, **image("https://cdn.example/new.jpg")), more="c1")
        page_2 = grid(*old_posts, more="c2")
        page_3 = grid(feed("LATER", taken_at=old_stamp, **image("https://cdn.example/later.jpg")), more="")
        watch = FakeWatch({f"{SITE}/example_creator/": [page_2, page_3]})
        saved = read(options=Options(categories=("posts",), since=date(2023, 11, 14)), watch=watch,
                     pages={f"{SITE}/example_creator/": embedded(PROFILE, page_1)})
        self.assertEqual([item.path for item in saved], [f"posts/{DAY}_NEW.jpg"])
        self.assertEqual(watch.batches[f"{SITE}/example_creator/"], [page_3])

    # A sync's stop at saved posts is the engine's (`flnd.engine.job._queue_listed`), tested in
    # tests/test_job.py; the source no longer asks whether a post is saved.
    def test_the_tab_alone_has_no_early_stop(self):
        """Without the engine, the tab reads to its end, old posts and all: only the site ends it."""
        old_stamp = STAMP - 10 * 86400
        page_1 = grid(feed("NEW", **image("https://cdn.example/new.jpg")), more="c1")
        page_2 = grid(*[feed(f"OLD{i}", taken_at=old_stamp, **image(f"https://cdn.example/o{i}.jpg"))
                        for i in range(6)], more="")
        watch = FakeWatch({f"{SITE}/example_creator/": [page_2]})
        site = FakeSite(Target("ins:example_creator", kind="user"),
                        pages={f"{SITE}/example_creator/": embedded(page_1)},
                        options=Options(since=date(2023, 11, 14)))
        instance = Instagram()
        with watching(watch):
            listed = [post.id for post in instance.posts(site, instance.user(site, "example_creator"))]
        self.assertEqual(listed, ["NEW"] + [f"OLD{i}" for i in range(6)])

    def test_a_post_without_a_pk_opens_its_page_only_after_the_scroll(self):
        """fixes the tab being navigated mid-scroll: a page-only post is listed once the scroll ends."""
        page_1 = grid({"code": "GONE", "product_type": "clips", "taken_at": STAMP}, more="c1")
        page_2 = grid(feed("P2", **image("https://cdn.example/p2.jpg")), more="")
        watch = FakeWatch({f"{SITE}/example_creator/": [page_2]})
        seen = Seen()
        saved = read(options=Options(categories=("posts",)), watch=watch, seen=seen,
                     pages={f"{SITE}/example_creator/": embedded(page_1),
                            f"{SITE}/p/GONE/": page_of("GONE", taken_at=STAMP, **image("https://cdn.example/g.jpg"))})
        self.assertEqual([item.path for item in saved], [f"posts/{DAY}_P2.jpg", f"posts/{DAY}_GONE.jpg"])
        self.assertEqual(watch.batches[f"{SITE}/example_creator/"], [])
        self.assertEqual(seen.visited, [f"{SITE}/example_creator/", f"{SITE}/p/GONE/"])

    def test_complete_carousel_and_clips_in_grid_skip_lazy_api(self):
        car = {
            "code": "CAR", "pk": PK["CAR"], "product_type": "carousel_container", "taken_at": STAMP,
            "carousel_media": [image("https://cdn.example/c0.jpg"), video("https://cdn.example/c1.mp4")],
        }
        reel = {"code": "R1", "pk": PK["R1"], "product_type": "clips", "taken_at": STAMP,
                **video("https://cdn.example/r1.mp4")}
        seen = Seen()
        saved = read(options=Options(categories=("posts",)), watch=FakeWatch({}), seen=seen,
                     pages={f"{SITE}/example_creator/": embedded(PROFILE, grid(car, reel))})
        self.assertEqual(seen.requests, [])
        self.assertEqual([item.path for item in saved], [
            f"posts/{DAY}_CAR_0.jpg", f"posts/{DAY}_CAR_1.mp4", f"posts/{DAY}_R1.mp4",
        ])


class Landing(FakeSite):
    """A `FakeSite` whose tab lands elsewhere on a URL's first visits: `lands={url: [landing, …]}`."""

    def __init__(self, target, *, lands, **options):
        super().__init__(target, **options)
        self.lands = lands

    @property
    def url(self):
        url = self._seen.visited[-1] if self._seen.visited else "about:blank"
        visits = self._seen.visited.count(url)
        landings = self.lands.get(url, [])
        return landings[visits - 1] if 1 <= visits <= len(landings) else url


class ThrottleTest(unittest.TestCase):
    def test_a_please_wait_answer_is_waited_out_and_the_same_post_saved(self):
        answers = [SiteError("HTTP 400", status=400, response=_net.Response(
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
        # In the tab's order: T1, read where it is listed, then P1.
        self.assertEqual(
            [item.path for item in saved],
            [f"tagged/{DAY}_T1_0.jpg", f"tagged/{DAY}_T1_1.jpg", f"tagged/{DAY}_P1.jpg"],
        )

    def test_a_refusal_of_flnds_client_moves_the_calls_into_the_page(self):
        seen = Seen()

        def media(url, request):
            # flnd's own client is refused; the tab's fetch is answered.
            if not seen.requests[-1][2]["in_page"]:
                return SiteError("HTTP 403", status=403)
            return JSON[f"{API}/media/{PK['T1']}/info/"]

        saved = read(options=Options(categories=("tagged",)), json={**JSON, f"{API}/media/{PK['T1']}/info/": media},
                     seen=seen, cookies={"csrftoken": "tok", "sessionid": "s"})
        self.assertEqual([options["in_page"] for _, _, options in seen.requests], [False, True])
        self.assertEqual(seen.requests[1][2]["headers"]["X-CSRFToken"], "tok")
        self.assertEqual(seen.requests[1][2]["headers"]["X-IG-App-ID"], APP_ID)
        self.assertEqual(seen.back_offs, [])
        self.assertIn(f"tagged/{DAY}_T1_0.jpg", [item.path for item in saved])

    def test_a_403_the_engine_takes_for_a_login_wall_still_moves_the_calls_into_the_page(self):
        """site.fetch raises a 401 or 403 as a LoginError; the API reads it with check=False instead."""

        class Walling(FakeSite):
            def fetch(self, url, *, check=True, **options):
                response = super().fetch(url, check=False, **options)
                if check and response.status in (401, 403):
                    raise LoginError("ins: the site refused the login (HTTP 403)")
                return response

        seen = Seen()

        def media(url, request):
            if not seen.requests[-1][2]["in_page"]:
                return SiteError("HTTP 403", status=403)
            return JSON[f"{API}/media/{PK['T1']}/info/"]

        site = Walling(Target("ins:example_creator", kind="user"), seen=seen,
                       json={**JSON, f"{API}/media/{PK['T1']}/info/": media})
        source = Instagram()
        self.assertEqual(source.api.media(site, PK["T1"])["code"], "T1")
        self.assertEqual([options["in_page"] for _, _, options in seen.requests], [False, True])
        self.assertIn("! Instagram refused flnd's own client; sending its API calls from the page", seen.messages)

    def test_a_page_answered_to_the_api_moves_the_calls_into_the_page(self):
        """A 200 answer whose body is a page isn't an answer: the tab's own fetch is asked."""
        seen = Seen()

        def media(url, request):
            # flnd's own client is answered with a page; the tab's fetch with JSON.
            if not seen.requests[-1][2]["in_page"]:
                return SiteError("HTTP 200", status=200, response=_net.Response(
                    200, url, {"content-type": "text/html"}, b"<html></html>"))
            return JSON[f"{API}/media/{PK['T1']}/info/"]

        saved = read(options=Options(categories=("tagged",)),
                     json={**JSON, f"{API}/media/{PK['T1']}/info/": media}, seen=seen)
        self.assertEqual([options["in_page"] for _, _, options in seen.requests], [False, True])
        self.assertIn("! Instagram answered the API with a page; sending its API calls from the page",
                      seen.messages)
        self.assertIn(f"tagged/{DAY}_T1_0.jpg", [item.path for item in saved])

    def test_a_page_answered_from_the_tab_is_a_refusal(self):
        """From the tab, a page instead of an answer stays `Changed`: the callers read the page."""
        seen = Seen()
        page = SiteError("HTTP 200", status=200, response=_net.Response(
            200, f"{API}/media/{PK['T1']}/info/", {"content-type": "text/html"}, b"<html></html>"))
        site = FakeSite(seen=seen, json={f"{API}/media/{PK['T1']}/info/": page})
        with self.assertRaises(Changed):
            Instagram().api.media(site, PK["T1"])
        self.assertEqual([options["in_page"] for _, _, options in seen.requests], [False, True])

    def test_a_refused_media_call_reads_the_posts_page(self):
        seen = Seen()
        pages = {**PAGES, f"{SITE}/p/T1/": shortcode_page({"code": "T1", "taken_at": STAMP,
                                                           **image("https://cdn.example/t.jpg")})}
        refused = {**JSON, f"{API}/media/{PK['T1']}/info/": SiteError("HTTP 500", status=None)}
        saved = read(options=Options(categories=("tagged",)), json=refused, pages=pages, seen=seen)
        self.assertIn(f"tagged/{DAY}_T1.jpg", [item.path for item in saved])
        self.assertIn(f"{SITE}/p/T1/", seen.visited)
        self.assertIn("media API refused", "\n".join(seen.messages))

    def test_a_page_answered_to_the_media_call_reads_the_page_quietly(self):
        """A page instead of media JSON is the mode `get` announced once: no warning per post."""
        seen = Seen()
        page = SiteError("HTTP 200", status=200, response=_net.Response(
            200, f"{API}/media/{PK['T1']}/info/", {"content-type": "text/html"}, b"<html></html>"))
        pages = {**PAGES, f"{SITE}/p/T1/": shortcode_page({"code": "T1", "taken_at": STAMP,
                                                           **image("https://cdn.example/t.jpg")})}
        answered = {**JSON, f"{API}/media/{PK['T1']}/info/": page}
        saved = read(options=Options(categories=("tagged",)), json=answered, pages=pages, seen=seen)
        self.assertIn(f"tagged/{DAY}_T1.jpg", [item.path for item in saved])
        self.assertIn(f"{SITE}/p/T1/", seen.visited)
        self.assertNotIn("media API refused", "\n".join(seen.messages))

    def test_a_login_wall_is_waited_out_and_the_page_loaded_again(self):
        site = Landing(Target("ins:SINGLE", kind="post"), pages=PAGES,
                       lands={f"{SITE}/p/SINGLE/": [f"{SITE}/accounts/login/?next=/p/SINGLE/", f"{SITE}/challenge/"]})
        posts = list(Instagram().post(site, "SINGLE"))
        self.assertEqual([post.id for post in posts], ["SINGLE"])
        self.assertEqual(site._seen.visited, [f"{SITE}/p/SINGLE/"] * 3)
        self.assertEqual(site._seen.back_offs, ["Instagram showed a login wall"] * 2)

    def test_a_suspended_account_is_a_hard_stop(self):
        site = Landing(Target("ins:SINGLE", kind="post"), pages=PAGES,
                       lands={f"{SITE}/p/SINGLE/": [f"{SITE}/accounts/suspended/?next=/p/SINGLE/"]})
        with self.assertRaisesRegex(LoginError, "suspended or locked"):
            list(Instagram().post(site, "SINGLE"))
        suspended = SiteError("HTTP 403", status=403, response=_net.Response(
            403, f"{SITE}/accounts/suspended/", {"content-type": "text/html"}, b"<html></html>"))
        with self.assertRaisesRegex(LoginError, "suspended or locked"):
            read(options=Options(categories=("tagged",)), json={**JSON, f"{API}/media/{PK['T1']}/info/": suspended})

    def test_the_www_claim_is_kept_from_each_answer(self):
        source = Instagram()

        class Claiming(FakeSite):
            def fetch(self, url, **options):
                answer = super().fetch(url, **options)
                assert answer is not None
                return _net.Response(200, url, {"x-ig-set-www-claim": "hmac.new"}, answer.content)

        site = Claiming(Target("ins:example_creator", kind="user"), json=JSON)
        source.api.media(site, PK["R1"])
        self.assertEqual(source.api.claim, "hmac.new")

    def test_persistent_login_wall_in_load_raises_login_error(self):
        site = Landing(Target("ins:SINGLE", kind="post"), pages=PAGES,
                       lands={f"{SITE}/p/SINGLE/": [f"{SITE}/accounts/login/?next=/p/SINGLE/"] * 4})
        with self.assertRaisesRegex(LoginError, "login wall or challenge"):
            list(Instagram().post(site, "SINGLE"))
        self.assertEqual(site._seen.back_offs, ["Instagram showed a login wall"] * 2)

    def test_a_profile_page_without_its_user_is_loaded_once(self):
        from flnd.sources.ins import Profile

        site = FakeSite(Target("ins:example_user", kind="user"),
                        pages={f"{SITE}/example_user/": "<html></html>"})
        profile = Profile("example_user")
        for _ in range(3):
            profile.describe(site)
        self.assertEqual(site._seen.visited, [f"{SITE}/example_user/"])

    def test_persistent_login_wall_in_api_get_marks_media_refused_and_falls_back(self):
        seen = Seen()
        pages = {
            **PAGES,
            f"{SITE}/example_user/": embedded(PROFILE, grid(
                {"code": "CAR", "pk": PK["CAR"], "product_type": "carousel_container", "taken_at": STAMP},
                {"code": "R1", "pk": PK["R1"], "product_type": "clips", "taken_at": STAMP},
            )),
            f"{SITE}/p/CAR/": shortcode_page({"code": "CAR", "taken_at": STAMP,
                                              **image("https://cdn.example/car.jpg")}),
            f"{SITE}/p/R1/": shortcode_page({"code": "R1", "taken_at": STAMP,
                                             **video("https://cdn.example/r1.mp4")}),
        }
        walled = {"require_login": True, "status": "fail"}
        bad_json = {**JSON, f"{API}/media/{PK['CAR']}/info/": walled, f"{API}/media/{PK['R1']}/info/": walled}
        saved = read("ins:example_user", options=Options(categories=("posts",)), pages=pages,
                     json=bad_json, watch=FakeWatch({}), seen=seen)
        self.assertEqual([item.path for item in saved], [f"posts/{DAY}_CAR.jpg", f"posts/{DAY}_R1.mp4"])
        self.assertEqual(api_urls(seen).count(f"{API}/media/{PK['CAR']}/info/"), 4)
        self.assertEqual(api_urls(seen).count(f"{API}/media/{PK['R1']}/info/"), 0)
        # Both pages are opened after the scroll, never while the tab scrolls.
        self.assertEqual(seen.visited, [f"{SITE}/example_user/", f"{SITE}/p/CAR/", f"{SITE}/p/R1/"])
        self.assertEqual(seen.back_offs, ["Instagram showed a login wall"] * 2)

    def test_post_page_reads_captured_graphql_when_embedded_is_empty(self):
        pages = {f"{SITE}/p/SINGLE/": "<html></html>"}
        watch = FakeWatch({f"{SITE}/p/SINGLE/": [
            {"data": {"xig_fetch_media": {"code": "SINGLE", "taken_at": STAMP,
                                          **image("https://cdn.example/g.jpg")}}}
        ]})
        saved = read("https://www.instagram.com/p/SINGLE/", pages=pages, watch=watch)
        self.assertEqual([item.path for item in saved], [f"{DAY}_SINGLE.jpg"])


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


class MeTest(unittest.TestCase):
    def test_resolve_ins_me(self):
        target = Instagram().resolve("me")
        self.assertEqual(target, Target("ins:me", kind="me"))
        self.assertEqual(str(target), "ins:me")
        self.assertFalse(target.query)
        self.assertNotIn("me", Instagram.single_kinds)
        self.assertIn("ins:me", Instagram.forms)
        self.assertEqual(Instagram().resolve("https://www.instagram.com/me/"), Target("ins:me", kind="user"))

    def test_me_paginates_following_with_ds_user_id_cookie(self):
        seen = Seen()
        pages = [
            {"users": [{"username": "example_user"}], "next_max_id": "1"},
            {"users": [{"username": "example_creator"}], "next_max_id": None},
        ]
        json_map = {f"{API}/friendships/99/following/": lambda u, req: pages.pop(0)}
        site = FakeSite(Target("ins:me", kind="me"), cookies={"sessionid": "s", "ds_user_id": "99"}, json=json_map, seen=seen)
        found = list(Instagram().me(site, "me"))
        # Each account is a target as people write it; the engine resolves and downloads it.
        self.assertEqual(found, [Target("ins:example_user"), Target("ins:example_creator")])

    def test_me_falls_back_to_sessionid_prefix_and_current_user_api(self):
        json_map = {
            f"{API}/friendships/77/following/": {"users": [{"username": "example_user"}]},
            f"{API}/accounts/current_user/": {"user": {"pk": 88}},
            f"{API}/friendships/88/following/": {"users": [{"username": "example_handle"}]},
        }
        site_session = FakeSite(Target("ins:me", kind="me"), cookies={"sessionid": "77%3Aabc%3A2"}, json=json_map)
        self.assertEqual(list(Instagram().me(site_session, "me")), [Target("ins:example_user")])

        site_api = FakeSite(Target("ins:me", kind="me"), cookies={"sessionid": "opaque"}, json=json_map)
        self.assertEqual(list(Instagram().me(site_api, "me")), [Target("ins:example_handle")])

    def test_me_rejects_unexpected_response(self):
        site = FakeSite(Target("ins:me", kind="me"), cookies={"sessionid": "s", "ds_user_id": "99"}, json={f"{API}/friendships/99/following/": {}})
        with self.assertRaises(SiteError):
            list(Instagram().me(site, "me"))

    def test_engine_treats_ins_me_as_query_target(self):
        from flnd.engine import home as _home
        from flnd.source import File, Post
        from flnkit import cookies as _cookies

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            following_pages = [
                {"users": [{"username": "example_user"}]},
                {"users": [{"username": "example_creator"}, {"username": "example_user"}]},
            ]
            step = {"i": 0}

            def handler(request: _net.Request) -> _net.Reply:
                url = str(request.url).split("?", 1)[0]
                if url == f"{API}/friendships/99/following/":
                    body = following_pages[min(step["i"], len(following_pages) - 1)]
                    return _net.reply(200, json=body)
                if url.startswith("https://cdn.example/"):
                    return _net.reply(200, content=b"IMG")
                return _net.reply(404)

            def fake_read_tab(profile, site, api, tab):
                code = "A1" if profile.user == "example_user" else "B1"
                yield Post(code, [File(f"https://cdn.example/{code.lower()}.jpg")], date=day(STAMP))

            _home.save(home, "ins", _cookies.Jar([
                {"name": "sessionid", "value": "99:abc:1", "domain": ".instagram.com", "path": "/"},
            ]))
            engine = Engine(home=home, sources={"ins": Instagram})
            with _net.fake(handler), mock.patch("flnd.sources.ins.Profile.read_tab", fake_read_tab):
                dry = engine.download(["ins:me"], categories="posts", dry=True)
                self.assertTrue(dry.ok)
                self.assertEqual(fakes.targets(home), {})

                report = engine.download(["ins:me"], categories="posts", sync=True)
                self.assertTrue(report.ok)
                rows = fakes.targets(home)
                self.assertEqual(list(rows), ["ins:me"])
                self.assertEqual(rows["ins:me"].seen, ["ins:example_user"])
                ls = engine.target_ls()
                self.assertEqual(len(ls), 1)
                self.assertEqual(str(ls[0].target), "ins:me")
                self.assertTrue(ls[0].query)
                self.assertEqual(ls[0].seen, 1)
                self.assertTrue((home / "media" / "ins" / "example_user" / "posts" / f"{DAY}_A1.jpg").exists())

                step["i"] = 1
                sync_report = engine.sync()
                self.assertTrue(sync_report.ok)
                self.assertEqual(len(sync_report.targets), 1)
                self.assertEqual(str(sync_report.targets[0].target), "ins:me")
                self.assertEqual(list(fakes.targets(home)), ["ins:me"])
                self.assertTrue((home / "media" / "ins" / "example_creator" / "posts" / f"{DAY}_B1.jpg").exists())


if __name__ == "__main__":
    unittest.main()
