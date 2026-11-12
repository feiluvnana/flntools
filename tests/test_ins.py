"""Instagram: tabs, posts read again from their page, stories, highlights, the picture, and one post. No network."""

import json
import os
import time
import unittest
from datetime import date
from unittest import mock

from fakes import FakeSite, Seen, collect
from test_dash import INSTAGRAM

from flnd.session import Options
from flnd.source import Target, TargetError
from flnd.sources.common.relay import embedded_results
from flnd.sources.ins import Instagram, best_file, files_of, is_complete, post_files, tab_media
from shared import selector

SITE = "https://www.instagram.com"
# 2023-11-15 12:00 UTC: the same day in every time zone, so a name doesn't depend on where the tests run.
STAMP = 1700049600
DAY = "2023-11-15"


def embedded(*results):
    """A page whose HTML embeds `results` as Instagram's server does."""
    blocks = "".join(f'<script type="application/json">{json.dumps({"require": [["x", {"__bbox": {"result": result}}]]})}'
                     "</script>" for result in results)
    return f"<html><body>{blocks}<script type='application/json'>{{'__bbox': not json</script></body></html>"


def image(url, width=640):
    return {"image_versions2": {"candidates": [{"url": url.replace(".jpg", "_s.jpg"), "width": 150, "height": 150},
                                               {"url": url, "width": width, "height": width}]}}


def video(url):
    return {**image(url.replace(".mp4", ".jpg")),
            "video_versions": [{"url": url.replace(".mp4", "_low.mp4"), "width": 480, "height": 854},
                               {"url": url, "width": 720, "height": 1280}]}


def grid(*nodes):
    return {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {"edges": [{"node": node} for node in nodes]}}}


def reels_grid(*codes):
    return {"data": {"fetch__XDTUserDict": {"clips_connection": {"edges": [
        {"node": {"media": {"code": code, **image(f"https://cdn.example/{code}_thumb.jpg")}}} for code in codes]}}}}


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

    def test_grid_listings_without_a_date_are_incomplete(self):
        self.assertFalse(is_complete(tab_media(reels_grid("R1"), "reels")[0]))
        self.assertTrue(is_complete({"taken_at": STAMP}))

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


def page_of(code, **media):
    return embedded({"data": {"xig_fetch_media": {"code": code, **media}}})


def shortcode_page(*posts):
    return embedded({"data": {"xdt_api__v1__media__shortcode__web_info": {"items": list(posts)}}})


def story_page(*stories):
    return embedded({"data": {"xdt_api__v1__feed__reels_media": {"reels_media": [{"items": list(stories)}]}}})


# What scrolling each tab of `someone` fetches.
GRIDS = {
    "posts": grid(
        {"code": "P1", "taken_at": STAMP, **image("https://cdn.example/p1.jpg")},
        {"code": "CAR", "taken_at": STAMP, "carousel_media": [image("https://cdn.example/c0.jpg"),
                                                             {**video("https://cdn.example/c1.mp4"),
                                                              "video_dash_manifest": INSTAGRAM}]},
        {"code": "R1", "taken_at": STAMP, **video("https://cdn.example/r1.mp4")},
        {"code": "WEB", "taken_at": STAMP, **image("https://cdn.example/w.webp")},
        {"code": "BARE", "taken_at": STAMP, **image("https://cdn.example/media/bare")},
        {"code": "NOFILE", "taken_at": STAMP}),
    "reels": reels_grid("R1", "R2", "GONE"),
    "tagged": {"data": {"xdt_api__v1__usertags__user_id__feed_connection": {"edges": [
        {"node": {"code": "T1"}}, {"node": {"code": "P1", "taken_at": STAMP, **image("https://cdn.example/p1.jpg")}}]}}},
    "reposts": {"data": {"fetch__XDTUserDict": {"user_reposts_timeline": {"repost_grid_items": [
        {"media": {"code": "RP1", "taken_at": STAMP, **image("https://cdn.example/rp1.jpg")}},
        {"media": {"code": "RP2"}}]}}}},
}
TABS = {"posts": "", "reels": "reels/", "tagged": "tagged/", "reposts": "reposts/"}
PAGES = {
    f"{SITE}/someone/": embedded(PROFILE, HIGHLIGHTS),
    f"{SITE}/p/R2/": page_of("R2", taken_at=STAMP, **video("https://cdn.example/r2.mp4")),
    f"{SITE}/p/GONE/": "<html></html>",
    f"{SITE}/p/T1/": shortcode_page({"code": "T1", "taken_at": STAMP, "carousel_media": [
        image("https://cdn.example/t0.jpg"), image("https://cdn.example/t1.jpg")]}),
    f"{SITE}/p/RP2/": page_of("RP2", taken_at=STAMP, **image("https://cdn.example/rp2.jpg")),
    f"{SITE}/stories/someone/": story_page({"pk": "5001", "taken_at": STAMP, **image("https://cdn.example/s.jpg")},
                                           {"pk": "5003_42", **video("https://cdn.example/s3.mp4")}),
    f"{SITE}/stories/highlights/9001/": embedded({"data": {"xdt_api__v1__feed__reels_media__connection": {"edges": [
        {"node": {"items": [{"id": "5002_42", "taken_at": STAMP, **video("https://cdn.example/h.mp4")},
                            {"id": "5004", "taken_at": STAMP, **image("https://cdn.example/h2.jpg")}]}}]}}}),
    f"{SITE}/p/SINGLE/": shortcode_page({"code": "SINGLE", "taken_at": STAMP, "carousel_media": [
        image("https://cdn.example/a.jpg"), {**video("https://cdn.example/r.mp4"), "video_dash_manifest": INSTAGRAM}]}),
}


class FakeWatch:
    """`watch_responses` over a fake tab: scrolling gives the responses held for the URL last loaded."""

    def __init__(self, batches):
        self.batches = batches

    def __call__(self, site, match, *, scroll=False, lines=False):
        batches = self.batches

        class Responses:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                pass

            def __iter__(self):
                # Read once the page is loaded, as the first scroll would.
                return iter(batches.get(site.page.url, []))

        return Responses()


def scrolled(*tabs):
    """The responses scrolling each of `tabs` of `someone` fetches."""
    return FakeWatch({f"{SITE}/someone/{TABS[tab]}": [GRIDS[tab]] for tab in tabs})


def read(text="ins:someone", *, options=Options(), watch=None, pages=PAGES, seen=None):
    """What the job would download for `text`, with `watch` as the tabs' scrolling (every tab's by default)."""
    with mock.patch("flnd.sources.ins.watch_responses", watch or scrolled(*TABS)):
        return collect(Instagram, text, pages=pages, options=options, seen=seen)


# What ins named and keyed before routes, recorded with the old `FakeSession` and `run(session)` in
# a UTC time zone over these fixtures. They are sorted: the old run opened every lazy post after
# the last tab, and the new one opens each tab's when the tab is done.
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


class NamesAndKeysTest(unittest.TestCase):
    def test_a_profile_is_named_and_keyed_as_before_routes(self):
        saved = read(options=Options(categories=("all",)))
        self.assertEqual(sorted((item.path, item.key) for item in saved), OLD_PROFILE)

    def test_a_post_is_named_and_keyed_as_before_routes(self):
        saved = read("https://www.instagram.com/p/SINGLE/")
        self.assertEqual([(item.path, item.key) for item in saved], OLD_SINGLE)
        self.assertEqual((saved[1].file.url, saved[1].file.audio),
                         ("https://cdn.example/1080.mp4", "https://cdn.example/a95.mp4"))

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

    def test_a_grid_post_without_details_is_lazy_and_a_listed_one_is_not_opened(self):
        site = FakeSite(Target("ins", "user", "someone"), pages=PAGES)
        instance = Instagram()
        with mock.patch("flnd.sources.ins.watch_responses", scrolled(*TABS)):
            listed = list(instance.reels(site, instance.user(site, "someone")))
        self.assertEqual([(post.id, post.lazy, post.date) for post in listed],
                         [("R1", True, None), ("R2", True, None), ("GONE", True, None)])

    def test_a_reel_the_posts_grid_lists_is_not_opened(self):
        seen = Seen()
        read(options=Options(categories=("posts", "reels")), seen=seen)
        # R1 is on the posts grid already, so only R2 and the gone post are opened.
        self.assertEqual(seen.visited, [f"{SITE}/someone/", f"{SITE}/someone/reels/", f"{SITE}/p/R2/", f"{SITE}/p/GONE/"])
        self.assertIn("! post GONE: its page holds no post; skipped", seen.messages)
        self.assertIn("reels: 3 posts listed", seen.messages)

    def test_a_lazy_post_outside_time_downloads_nothing(self):
        options = Options(categories=("reels",), since=date(2023, 11, 16))
        self.assertEqual(read(options=options), [])

    def test_the_profile_alone_loads_the_profile_page_once(self):
        seen = Seen()
        saved = read(options=Options(categories=("profile",)), seen=seen)
        self.assertEqual(seen.visited, [f"{SITE}/someone/"])
        self.assertEqual([(item.path, item.key) for item in saved], [("profile/123_456_n.jpg", "profile/123_456_n.jpg")])

    def test_highlights_and_the_picture_share_one_load_of_the_profile(self):
        seen = Seen()
        read(options=Options(categories=("highlights", "profile")), seen=seen)
        self.assertEqual(seen.visited, [f"{SITE}/someone/", f"{SITE}/stories/highlights/9001/"])

    def test_a_throttled_response_backs_off_and_the_scroll_goes_on(self):
        seen = Seen()
        throttled = {"message": "Please wait a few minutes before you try again.", "status": "fail"}
        watch = FakeWatch({f"{SITE}/someone/": [throttled, GRIDS["posts"]]})
        saved = read(options=Options(categories=("posts",)), watch=watch, seen=seen)
        self.assertEqual(seen.back_offs, ["Instagram asked to wait a few minutes"])
        self.assertIn(f"posts/{DAY}_P1.jpg", [item.path for item in saved])


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


if __name__ == "__main__":
    unittest.main()
