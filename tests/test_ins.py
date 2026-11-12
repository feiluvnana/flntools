"""Instagram: tabs, posts read again from their page, stories, highlights, the picture, and one post. No network."""

import json
import unittest
from datetime import date, datetime

from fakes import FakeSession
from test_dash import INSTAGRAM

from flnd.session import Options
from flnd.source import Target, TargetError
from flnd.sources.common.relay import embedded_results
from flnd.sources.ins import Instagram, best_file, is_complete, post_files, tab_media, taken_on
from shared import selector

SITE = "https://www.instagram.com"
STAMP = 1700000000
DAY = datetime.fromtimestamp(STAMP).date()


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
        self.assertEqual(taken_on(post), DAY)
        self.assertIsNone(taken_on({}))

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


class ProfileTest(unittest.TestCase):
    def run_profile(self, *, options=Options(categories=("all",)), on_scroll=None, pages=None):
        session = FakeSession(Target("ins", "user", "someone"), options=options, on_scroll=on_scroll,
                              pages={f"{SITE}/someone/": embedded(PROFILE, HIGHLIGHTS), **(pages or {})})
        Instagram().run(session)
        return session

    def test_every_category(self):
        def scrolled(session):
            if session.folder == "posts":
                session.respond(f"{SITE}/graphql/query", grid(
                    {"code": "P1", "taken_at": STAMP, **image("https://cdn.example/p1.jpg")},
                    {"code": "R1", "taken_at": STAMP, **video("https://cdn.example/r1.mp4")}))
            if session.folder == "reels":
                session.respond(f"{SITE}/graphql/query", reels_grid("R1", "R2"))

        reel_page = embedded({"data": {"xig_fetch_media": {"code": "R2", "taken_at": STAMP,
                                                           **video("https://cdn.example/r2.mp4")}}})
        stories = embedded({"data": {"xdt_api__v1__feed__reels_media": {"reels_media": [{"items": [
            {"pk": "5001", "taken_at": STAMP, **image("https://cdn.example/s.jpg")}]}]}}})
        highlight = embedded({"data": {"xdt_api__v1__feed__reels_media__connection": {"edges": [{"node": {"items": [
            {"id": "5002_42", "taken_at": STAMP, **video("https://cdn.example/h.mp4")}]}}]}}})
        session = self.run_profile(on_scroll=scrolled, pages={f"{SITE}/p/R2/": reel_page,
                                                               f"{SITE}/stories/someone/": stories,
                                                               f"{SITE}/stories/highlights/9001/": highlight})
        self.assertEqual(session.paths(), [
            f"posts/{DAY}_P1.jpg", f"posts/{DAY}_R1.mp4", f"reels/{DAY}_R2.mp4", "profile/123_456_n.jpg",
            f"stories/{DAY}_5001.jpg", f"highlights/Trip_2026/{DAY}_5002.mp4"])
        self.assertEqual([item["category"] for item in session.saved],
                         ["posts", "posts", "reels", "profile", "stories", "highlights"])
        self.assertEqual(session.saved[1]["source"], "https://cdn.example/r1.mp4")
        self.assertIsNone(session.saved[1]["audio"])
        self.assertEqual(session.saved[0]["headers"], {"Referer": f"{SITE}/"})
        # R1 was on the grid already, so only R2 is opened.
        self.assertNotIn(f"{SITE}/p/R1/", session.visited)
        self.assertIsNone(session.folder)

    def test_without_from_every_tab_is_read(self):
        session = self.run_profile(options=Options())
        self.assertEqual(session.visited, self.run_profile().visited)
        self.assertIn(f"{SITE}/someone/tagged/", session.visited)
        self.assertIn(f"{SITE}/someone/reposts/", session.visited)

    def test_the_profile_alone_loads_the_profile_page_once(self):
        session = self.run_profile(options=Options(categories=("profile",)))
        self.assertEqual(session.visited, [f"{SITE}/someone/"])
        self.assertEqual(session.paths(), ["profile/123_456_n.jpg"])

    def test_a_post_page_without_a_post_is_a_warning(self):
        def scrolled(session):
            session.respond(f"{SITE}/graphql/query", reels_grid("GONE"))

        session = self.run_profile(options=Options(categories=("reels",)), on_scroll=scrolled)
        self.assertIn(f"{SITE}/p/GONE/", session.visited)
        self.assertIn("! post GONE: its page holds no post; skipped", session.messages)

    def test_late_results_are_ignored(self):
        session = self.run_profile(options=Options(categories=("posts",)))
        session.respond(f"{SITE}/graphql/query", grid({"code": "LATE", "taken_at": STAMP, **image("https://c/x.jpg")}))
        self.assertEqual(session.saved, [])


class SinglePostTest(unittest.TestCase):
    def test_a_carousel_post_from_its_page(self):
        post = {"code": "C1abc", "taken_at": STAMP, "carousel_media": [image("https://cdn.example/a.jpg"),
                                                                     image("https://cdn.example/b.jpg")]}
        page = embedded({"data": {"xdt_api__v1__media__shortcode__web_info": {"items": [post]}}})
        session = FakeSession(Target("ins", "post", "C1abc"), pages={f"{SITE}/p/C1abc/": page})
        Instagram().run(session)
        self.assertEqual(session.paths(), [f"{DAY}_C1abc_0.jpg", f"{DAY}_C1abc_1.jpg"])

    def test_a_reel_with_a_manifest_is_saved_from_dash(self):
        reel = {"code": "R9", "taken_at": STAMP, **video("https://cdn.example/r.mp4"), "video_dash_manifest": INSTAGRAM}
        page = embedded({"data": {"xig_fetch_media": reel}})
        session = FakeSession(Target("ins", "post", "R9"), pages={f"{SITE}/p/R9/": page})
        Instagram().run(session)
        self.assertEqual(session.paths(), [f"{DAY}_R9.mp4"])
        self.assertEqual((session.saved[0]["source"], session.saved[0]["audio"]),
                         ("https://cdn.example/1080.mp4", "https://cdn.example/a95.mp4"))
        self.assertEqual(session.saved[0]["date"], date.fromisoformat(str(DAY)))


if __name__ == "__main__":
    unittest.main()
