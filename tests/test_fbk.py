"""Facebook: the profile header, its tabs, the media walker, stories and pictures. No network, no browser."""

import json
import unittest
from datetime import datetime

from fakes import FakeSession
from test_dash import INSTAGRAM

from flnd.session import Options
from flnd.source import Target, TargetError
from flnd.sources.fbk import Facebook, image_extension, media_items

SITE = "https://www.facebook.com"
STAMP = 1700000000
DAY = datetime.fromtimestamp(STAMP).date()
USER = "user"
SINGLE = "single posts and videos aren't supported yet"

# Every form fbk reads, as people write it, and what it becomes: (kind, value), or a refusal's text.
# The kinds and values are the keys of targets.db and the history, so a change here is a migration.
ROUTES = [
    ("fbk:example.page", (USER, "example.page")),
    ("fbk:@example.page", (USER, "example.page")),
    ("fbk:100012345678901", (USER, "100012345678901")),
    ("fbk:facebook.com/example.page", (USER, "example.page")),
    ("https://www.facebook.com/example.page", (USER, "example.page")),
    ("https://facebook.com/example.page/", (USER, "example.page")),
    ("https://www.facebook.com/@example.page", (USER, "example.page")),
    ("https://fb.com/example.page", (USER, "example.page")),
    ("https://m.facebook.com/example.page/photos", (USER, "example.page")),
    ("https://mobile.facebook.com/example.page", (USER, "example.page")),
    # A profile's tab is the profile.
    ("https://www.facebook.com/example.page/photos", (USER, "example.page")),
    ("https://www.facebook.com/example.page/photos_by", (USER, "example.page")),
    ("https://www.facebook.com/example.page/photos_of", (USER, "example.page")),
    ("https://www.facebook.com/example.page/photos_albums", (USER, "example.page")),
    ("https://www.facebook.com/example.page/photos/a.1/2", (USER, "example.page")),
    ("https://www.facebook.com/example.page/reels_tab", (USER, "example.page")),
    ("https://www.facebook.com/example.page/videos_by", (USER, "example.page")),
    ("https://www.facebook.com/example.page/videos/2", (USER, "example.page")),
    ("https://www.facebook.com/profile.php?id=100012345678901", (USER, "100012345678901")),
    # Changed: a profile's id is digits (`{user:\d+}`), so a non-numeric id is refused, where it was read as a name.
    ("https://www.facebook.com/profile.php?id=abc", "can't read"),
    # A name that starts like a page of the site is still a profile.
    ("https://www.facebook.com/watchman", (USER, "watchman")),
    ("https://www.facebook.com/example.page/posts/123", SINGLE),
    ("https://www.facebook.com/example.page/posts", SINGLE),
    # Changed: `about` isn't a tab, so it now reads the profile. Only the listed pages are refused.
    ("https://www.facebook.com/example.page/about", (USER, "example.page")),
    ("https://www.facebook.com/watch/?v=123", SINGLE),
    ("fbk:facebook.com/watch/?v=1", SINGLE),
    ("https://fb.com/example.page/posts/1", SINGLE),
    ("https://fb.com/watch/?v=1", SINGLE),
    ("https://fb.com/reel/1", SINGLE),
    ("https://fb.com/reels/1", SINGLE),
    ("https://fb.com/videos/1", SINGLE),
    ("https://fb.com/photo/?fbid=1", SINGLE),
    ("https://fb.com/photos/1", SINGLE),
    ("https://fb.com/groups/x", SINGLE),
    ("https://fb.com/events/x", SINGLE),
    ("https://fb.com/stories/x/1", SINGLE),
    ("https://fb.com/story.php?story_fbid=1", SINGLE),
    ("https://fb.com/profile.php?id=100012345678901", (USER, "100012345678901")),
    ("https://fb.com/people/example/100012345678901", (USER, "100012345678901")),
    ("https://fb.com/@example.page", (USER, "example.page")),
    ("https://www.facebook.com/reel/123", SINGLE),
    ("https://www.facebook.com/reels/123", SINGLE),
    ("https://www.facebook.com/videos/123", SINGLE),
    ("https://www.facebook.com/photo/?fbid=1", SINGLE),
    ("https://www.facebook.com/photos/abc", SINGLE),
    ("https://www.facebook.com/groups/x", SINGLE),
    ("https://www.facebook.com/events/x", SINGLE),
    ("https://www.facebook.com/stories/x/1", SINGLE),
    ("https://www.facebook.com/story.php?story_fbid=1", SINGLE),
    # Changed: `people/<id>` is the new route. A `people` path without an id is refused, with the
    # "can't read" message rather than SINGLE, since the name pattern skips `people`.
    ("https://www.facebook.com/people/example", "(single posts and videos|can't read)"),
    ("https://www.facebook.com/people/example/100012345678901", (USER, "100012345678901")),
    ("https://www.facebook.com/profile.php", "can't read"),
    ("https://www.facebook.com/", "can't read"),
    ("fbk:example.com/example.page", "can't read"),
    ("fbk:not a name", "can't read"),
    ("fbk:ex-ample", "can't read"),
]


def embedded(*results):
    blocks = "".join(f'<script type="application/json">{json.dumps({"require": [["x", {"__bbox": {"result": result}}]]})}'
                     "</script>" for result in results)
    return f"<html><body>{blocks}</body></html>"


def header(*tab_keys, buckets=("900",)):
    sections = [{"node": {"tab_key": key, "all_collections": {"nodes": []}}} for key in tab_keys]
    return {"data": {"user": {"profile_header_renderer": {"user": {
        "profilePicLarge": {"uri": "https://cdn.example/v/pic.jpg?stp=1"}, "profilePhoto": {"id": "11"},
        "cover_photo": {"photo": {"id": "12", "image": {"uri": "https://cdn.example/v/cover.png"}}},
        "story_bucket": {"nodes": [{"id": bucket} for bucket in buckets]},
        "profile_tabs": {"profile_user": {"timeline_nav_app_sections": {"edges": sections}}},
    }}}}}


PHOTO = {"__typename": "Photo", "id": "21", "creation_time": STAMP, "viewer_image": {"uri": "https://cdn.example/1.jpg"},
         "album": {"name": "Summer: 2026"}}
VIDEO = {"__typename": "Video", "id": "22", "publish_time": STAMP, "videoDeliveryResponseFragment": {
    "videoDeliveryResponseResult": {"progressive_urls": [
        {"progressive_url": "https://cdn.example/sd.mp4", "metadata": {"quality": "SD"}},
        {"progressive_url": "https://cdn.example/hd.mp4", "metadata": {"quality": "HD"}}]}},
    "playable_url": "https://cdn.example/sd.mp4"}
STORY = {"data": {"bucket": {"unified_stories_with_notes": {"edges": [
    {"node": {"creation_time": str(STAMP), "attachments": [{"media": {**VIDEO, "id": "31"}}]}},
    {"node": {"creation_time": str(STAMP), "attachments": [{"media": {"__typename": "Photo", "id": "32",
                                                                      "image": {"uri": "https://cdn.example/s.jpg"}}}]}},
]}}}}


def with_dash(node, manifest):
    result = dict(node["videoDeliveryResponseFragment"]["videoDeliveryResponseResult"],
                  dash_manifests=[{"manifest_xml": manifest, "failure_reason": None}])
    return {**node, "videoDeliveryResponseFragment": {"videoDeliveryResponseResult": result}}


class RoutesTest(unittest.TestCase):
    def test_every_form(self):
        for text, expected in ROUTES:
            with self.subTest(text=text):
                value = text.removeprefix("fbk:")
                if isinstance(expected, tuple):
                    self.assertEqual(Facebook().resolve(value), Target("fbk", *expected))
                else:
                    with self.assertRaisesRegex(TargetError, f"^fbk: {expected}"):
                        Facebook().resolve(value)


class WalkerTest(unittest.TestCase):
    def test_a_dash_manifest_gives_its_best_video_and_audio(self):
        [item] = media_items({"data": {"node": with_dash(VIDEO, INSTAGRAM)}})
        self.assertEqual((item.url, item.audio), ("https://cdn.example/1080.mp4", "https://cdn.example/a95.mp4"))
        [item] = media_items({"data": {"node": with_dash(VIDEO, "<MPD/>")}})
        self.assertEqual((item.url, item.audio), ("https://cdn.example/hd.mp4", None))

    def test_photos_and_videos_are_found_once_with_their_dates(self):
        items = list(media_items({"data": {"a": PHOTO, "b": [VIDEO, VIDEO], "c": {"id": "x", "image": {"w": 1}}}}))
        self.assertEqual([(item.id, item.kind, item.day, item.album) for item in items],
                         [("21", "image", DAY, "Summer: 2026"), ("22", "video", DAY, None)])
        self.assertEqual(items[1].url, "https://cdn.example/hd.mp4")

    def test_a_videos_thumbnails_are_not_photos(self):
        video = {**VIDEO, "preferred_thumbnail": {"id": "23", "image": {"uri": "https://cdn.example/thumb.jpg"}}}
        self.assertEqual([item.id for item in media_items({"data": {"node": video}})], ["22"])

    def test_media_another_account_owns_is_left_out(self):
        theirs = {**VIDEO, "id": "24", "owner": {"id": "999"}}
        ours = {**PHOTO, "owner": {"id": "42"}}
        self.assertEqual([item.id for item in media_items({"data": [theirs, ours]}, owner="42")], ["21"])
        self.assertEqual(len(list(media_items({"data": [theirs, ours]}))), 2)

    def test_the_header_is_not_a_tab_photo(self):
        self.assertEqual(list(media_items(header("photos"))), [])

    def test_grid_tiles_are_skipped_for_the_photo_inside(self):
        tile = {"__typename": "TimelineAppCollectionItem", "id": "YXBw", "image": {"uri": "https://cdn.example/small.jpg"},
                "node": PHOTO}
        self.assertEqual([item.id for item in media_items({"data": {"edges": [{"node": tile}]}})], ["21"])

    def test_the_extension_is_the_format_the_cdn_sends(self):
        self.assertEqual(image_extension("https://cdn.example/v/a_n.png?stp=dst-jpg_s206x206&x=1"), "jpg")
        self.assertEqual(image_extension("https://cdn.example/v/a_n.png?stp=dst-png"), "png")
        self.assertEqual(image_extension("https://cdn.example/v/a_n.webp"), "webp")
        self.assertEqual(image_extension("https://cdn.example/v/a"), "jpg")


class ProfileTest(unittest.TestCase):
    def test_every_category(self):
        def scrolled(session):
            if session.folder == "photos":
                session.respond(f"{SITE}/api/graphql/", {"data": {"node": PHOTO}})
            if session.folder == "reels":
                session.respond(f"{SITE}/api/graphql/", {"data": {"node": VIDEO}})
                session.respond(f"{SITE}/api/graphql/", ["not a dict"])

        session = FakeSession(Target("fbk", "user", "example.page"), options=Options(categories=("all",)),
                              on_scroll=scrolled,
                              pages={f"{SITE}/example.page": embedded(header("photos_by", "photos_albums", "reels_tab")),
                                     f"{SITE}/stories/900/": embedded(STORY)})
        Facebook().run(session)
        self.assertEqual(session.visited, [f"{SITE}/example.page", f"{SITE}/example.page/photos_by",
                                           f"{SITE}/example.page/photos_albums", f"{SITE}/example.page/reels_tab",
                                           f"{SITE}/stories/900/"])
        self.assertEqual(session.paths(), [f"albums/Summer_2026/{DAY}_21.jpg", f"reels/{DAY}_22.mp4",
                                           "profile/profile_11.jpg", "profile/cover_12.png",
                                           f"stories/{DAY}_31.mp4", f"stories/{DAY}_32.jpg"])
        self.assertEqual([item["category"] for item in session.saved],
                         ["photos", "reels", "profile", "profile", "stories", "stories"])
        self.assertEqual(session.saved[0]["headers"], {"Referer": f"{SITE}/"})
        self.assertEqual([item["audio"] for item in session.saved], [None] * 6)
        self.assertIn("tagged: this profile has no such tab", session.messages)

    def test_dash_videos_are_saved_with_their_audio(self):
        story = {"data": {"bucket": {"unified_stories_with_notes": {"edges": [{"node": {
            "creation_time": str(STAMP), "attachments": [{"media": {**with_dash(VIDEO, INSTAGRAM), "id": "31"}}]}}]}}}}

        def scrolled(session):
            session.respond(f"{SITE}/api/graphql/", {"data": {"node": with_dash(VIDEO, INSTAGRAM)}})

        session = FakeSession(Target("fbk", "user", "example.page"), options=Options(categories=("reels", "stories")),
                              on_scroll=scrolled,
                              pages={f"{SITE}/example.page": embedded(header("reels_tab")),
                                     f"{SITE}/stories/900/": embedded(story)})
        Facebook().run(session)
        self.assertEqual(session.paths(), [f"reels/{DAY}_22.mp4", f"stories/{DAY}_31.mp4"])
        self.assertEqual([item["audio"] for item in session.saved], ["https://cdn.example/a95.mp4"] * 2)
        self.assertIsNone(session.folder)

    def test_each_album_is_opened_into_its_folder(self):
        album_url = f"{SITE}/media/set/?set=a.5&type=3"
        tiles = {"data": {"node": {"edges": [{"node": {"__typename": "TimelineAppCollectionItem", "url": album_url,
                                                       "title": {"text": "Trip"}, "node": {"__typename": "Album"}}}]}}}
        in_album = {"__typename": "Photo", "id": "41", "image": {"uri": "https://cdn.example/41.jpg"}}
        session = FakeSession(Target("fbk", "user", "a"), options=Options(categories=("albums",)),
                              pages={f"{SITE}/a": embedded(header("photos_albums")),
                                     f"{SITE}/a/photos_albums": embedded(tiles), album_url: embedded({"data": in_album})})
        Facebook().run(session)
        self.assertEqual(session.visited, [f"{SITE}/a", f"{SITE}/a/photos_albums", album_url])
        self.assertEqual(session.paths(), ["albums/Trip/41.jpg"])

    def test_without_a_header_every_tab_is_tried(self):
        session = FakeSession(Target("fbk", "user", "a"), options=Options(categories=("videos",)))
        Facebook().run(session)
        self.assertEqual(session.visited, [f"{SITE}/a", f"{SITE}/a/videos_by"])
        self.assertIn("! the profile header wasn't found; its pictures and stories are skipped", session.messages)

    def test_tagged_is_taken_without_from_and_left_out_by_another_from(self):
        pages = {f"{SITE}/a": embedded(header("photos_of"))}
        session = FakeSession(Target("fbk", "user", "a"), pages=pages)
        Facebook().run(session)
        self.assertIn(f"{SITE}/a/photos_of", session.visited)
        posts = FakeSession(Target("fbk", "user", "a"), options=Options(categories=("posts",)), pages=pages)
        Facebook().run(posts)
        self.assertNotIn(f"{SITE}/a/photos_of", posts.visited)
        tagged = FakeSession(Target("fbk", "user", "a"), options=Options(categories=("tagged",)), pages=pages)
        Facebook().run(tagged)
        self.assertEqual(tagged.visited, [f"{SITE}/a", f"{SITE}/a/photos_of"])

    def test_late_responses_are_ignored(self):
        session = FakeSession(Target("fbk", "user", "a"))
        Facebook().run(session)
        session.respond(f"{SITE}/api/graphql/", {"data": {"__typename": "Photo", "id": "1", "image": {"uri": "https://f/x.jpg"}}})
        self.assertEqual(session.saved, [])


if __name__ == "__main__":
    unittest.main()
