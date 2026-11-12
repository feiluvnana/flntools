"""Facebook: the routes, the profile header, the tabs and categories, the media walker, stories and pictures.

What it fakes: the site and the browser. `fakes.collect` runs the routes and categories on a `FakeSite`,
whose tab (`FeedSite`) delivers the pages' embedded results, the responses of a first load, and one
batch of responses per scroll. No network and no browser.
"""

import datetime
import json
import unittest
from unittest import mock

from fakes import FakeSite, Seen, collect
from test_dash import INSTAGRAM

from flnd.session import Options
from flnd.source import FetchError, LoginError, Target, TargetError
from flnd.sources.fbk import Facebook, image_extension, media_items
from shared import browser

SITE = "https://www.facebook.com"
STAMP = 1700000000
DAY = "2023-11-14"
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


def header(*tab_keys, buckets=("900",), user_id=None):
    sections = [{"node": {"tab_key": key, "all_collections": {"nodes": []}}} for key in tab_keys]
    return {"data": {"user": {"profile_header_renderer": {"user": {
        "id": user_id,
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


# A profile read end to end, for the (path, key) proof: every kind of file fbk saves. Its dates are
# noon UTC, so the old local-time day and the new UTC day agree wherever the tests run.
NOON = 1700049600


def photo(photo_id, uri="https://cdn.example/p.jpg", *, stamp=NOON, album=None, owner=None):
    node = {"__typename": "Photo", "id": photo_id, "viewer_image": {"uri": uri}}
    if stamp:
        node["creation_time"] = stamp
    if album:
        node["album"] = {"name": album}
    if owner:
        node["owner"] = {"id": owner}
    return {"data": {"node": node}}


def video(video_id, *, stamp=NOON, manifest=None):
    node = {**VIDEO, "id": video_id, "publish_time": stamp}
    return {"data": {"node": with_dash(node, manifest) if manifest else node}}


ALBUM_URLS = {"Trip": f"{SITE}/media/set/?set=a.5&type=3", "Home: 1": f"{SITE}/media/set/?set=a.6&type=3"}
TILES = {"data": {"node": {"edges": [{"node": {"__typename": "TimelineAppCollectionItem", "url": url,
                                                "title": {"text": title}, "node": {"__typename": "Album"}}}
                                     for title, url in ALBUM_URLS.items()]}}}
BUCKET = {"data": {"bucket": {"unified_stories_with_notes": {"edges": [
    {"node": {"creation_time": str(NOON), "attachments": [{"media": {**VIDEO, "id": "31"}}]}},
    {"node": {"creation_time": str(NOON), "attachments": [{"media": {"__typename": "Photo", "id": "32",
                                                                     "image": {"uri": "https://cdn.example/s.png"}}}]}},
    {"node": {"attachments": [{"media": {"__typename": "Photo", "id": "33",
                                         "image": {"uri": "https://cdn.example/s.jpg"}}}]}},
]}}}}
# What each URL's page embeds, as `embedded` writes it.
PROFILE_PAGES = {
    f"{SITE}/example.page": embedded(
        header("photos_by", "photos_of", "photos_albums", "reels_tab", "videos_by", user_id="42"),
        photo("51"), video("52"), photo("53", stamp=0), photo("54", "https://cdn.example/w.png?stp=dst-webp"),
        photo("55", owner="999")),
    f"{SITE}/example.page/photos_by": embedded(photo("61", album="Summer: 2026"), photo("62")),
    f"{SITE}/example.page/photos_of": embedded(photo("71")),
    f"{SITE}/example.page/photos_albums": embedded(TILES, photo("81")),
    ALBUM_URLS["Trip"]: embedded(photo("91"), photo("62", "https://cdn.example/dup.jpg")),
    ALBUM_URLS["Home: 1"]: embedded(photo("92", stamp=0)),
    f"{SITE}/example.page/reels_tab": embedded(video("22", manifest=INSTAGRAM)),
    f"{SITE}/example.page/videos_by": embedded(video("23")),
    f"{SITE}/stories/900/": embedded(BUCKET),
}
# What scrolling a tab brings, by the tab's name: one batch (a list of responses) per scroll.
PROFILE_SCROLLS = {
    "photos": [[photo("63"), photo("64", owner="999"), video("65")]],
    "reels": [[video("24"), video("22")]],
    "videos": [[photo("66")]],
}


# Where scrolling each tab happens, by the tab's category.
TAB_URLS = {"photos": f"{SITE}/example.page/photos_by", "reels": f"{SITE}/example.page/reels_tab",
            "videos": f"{SITE}/example.page/videos_by"}
PROFILE_SCROLLS_BY_URL = {TAB_URLS[tab]: batches for tab, batches in PROFILE_SCROLLS.items()}


class FeedPage:
    """A tab for `watch_responses`: it delivers `early` when the network is awaited and one batch of `scrolls` per scroll."""

    def __init__(self, site, scrolls, early, redirects):
        self.site = site
        self.scrolls = {url: list(batches) for url, batches in scrolls.items()}
        self.early = {url: list(batch) for url, batch in early.items()}
        self.redirects = redirects
        self.handlers = []
        self.scrolled = 0

    @property
    def _page(self):
        return FakeSite.page.fget(self.site)

    @property
    def document(self):
        return self._page.document

    @property
    def url(self):
        return self.redirects.get(self._page.url, self._page.url)

    def on_response(self, match, handler, *, lines=False):
        self.handlers.append(handler)
        return browser.Subscription(lambda: self.handlers.remove(handler) if handler in self.handlers else None)

    def deliver(self, batch):
        for result in batch:
            for handler in list(self.handlers):
                handler(result, None)

    def scroll(self, *, until=None, **options):
        self.scrolled += 1
        batches = self.scrolls.get(self._page.url, [])
        self.deliver(batches.pop(0) if batches else [])
        return 1

    def wait(self, css=None, *, idle=False, timeout=30.0):
        self.deliver(self.early.pop(self._page.url, []))


class FeedSite(FakeSite):
    """A `FakeSite` whose tab delivers responses; see `FeedPage`."""

    def __init__(self, *args, scrolls=(), early=(), redirects=(), **options):
        super().__init__(*args, **options)
        self.feed = FeedPage(self, dict(scrolls), dict(early), dict(redirects))

    @property
    def page(self):
        return self.feed


def read(text="fbk:example.page", *, pages=None, scrolls=None, early=None, redirects=None, seen=None, **options):
    """What the job would download for `text`, through a `FeedSite`; `options` are `Options`'."""
    def site(*args, **kwargs):
        return FeedSite(*args, scrolls=scrolls or {}, early=early or {}, redirects=redirects or {}, **kwargs)

    with mock.patch("fakes.FakeSite", site):
        return collect(Facebook, text, pages=PROFILE_PAGES if pages is None else pages, options=Options(**options),
                       seen=seen)


# What fbk saved before the port, from the old `run(session)` on the same pages and scrolls, as (path, history key).
# The port must give the same pairs: no history migration. The order differs: each category is read in turn, and the
# albums' own pages are now read with the albums tab, where `run` read them last.
TODAYS_PAIRS = [
    ("albums/2023-11-15_81.jpg", "81"),
    ("albums/Home_1/92.jpg", "92"),
    ("albums/Summer_2026/2023-11-15_61.jpg", "61"),
    ("albums/Trip/2023-11-15_91.jpg", "91"),
    ("photos/2023-11-15_62.jpg", "62"),
    ("photos/2023-11-15_63.jpg", "63"),
    ("posts/2023-11-15_51.jpg", "51"),
    ("posts/2023-11-15_54.webp", "54"),
    ("posts/2023-11-15_55.jpg", "55"),
    ("posts/53.jpg", "53"),
    ("profile/cover_12.png", "profile/cover_12.png"),
    ("profile/profile_11.jpg", "profile/profile_11.jpg"),
    ("reels/2023-11-15_22.mp4", "22"),
    ("reels/2023-11-15_24.mp4", "24"),
    ("stories/2023-11-15_31.mp4", "31"),
    ("stories/2023-11-15_32.png", "32"),
    ("stories/33.jpg", "33"),
    ("tagged/2023-11-15_71.jpg", "71"),
    ("videos/2023-11-15_23.mp4", "23"),
    ("videos/2023-11-15_52.mp4", "52"),
    ("videos/2023-11-15_65.mp4", "65"),
    ("videos/2023-11-15_66.jpg", "66"),
]


class PairsTest(unittest.TestCase):
    def test_every_path_and_key_is_what_the_old_run_saved(self):
        saved = read(scrolls=PROFILE_SCROLLS_BY_URL)
        self.assertEqual(sorted((s.path, s.key) for s in saved), TODAYS_PAIRS)
        self.assertEqual(len({s.path for s in saved}), len(saved))


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
        self.assertEqual([(item.id, item.kind, item.date, item.album) for item in items],
                         [("21", "image", STAMP, "Summer: 2026"), ("22", "video", STAMP, None)])
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
        seen = Seen()
        pages = {f"{SITE}/example.page": embedded(header("photos_by", "photos_albums", "reels_tab")),
                 f"{SITE}/stories/900/": embedded(STORY)}
        scrolls = {f"{SITE}/example.page/photos_by": [[{"data": {"node": PHOTO}}]],
                   f"{SITE}/example.page/reels_tab": [[{"data": {"node": VIDEO}}, ["not a dict"]]]}
        saved = read(pages=pages, scrolls=scrolls, seen=seen)
        self.assertEqual(seen.visited, [f"{SITE}/example.page", f"{SITE}/example.page/photos_by",
                                        f"{SITE}/example.page/photos_albums", f"{SITE}/example.page/reels_tab",
                                        f"{SITE}/stories/900/"])
        self.assertEqual([s.path for s in saved], [
            f"albums/Summer_2026/{DAY}_21.jpg", f"reels/{DAY}_22.mp4", f"stories/{DAY}_31.mp4",
            f"stories/{DAY}_32.jpg", "profile/profile_11.jpg", "profile/cover_12.png"])
        self.assertEqual([s.post.category for s in saved], ["photos", "reels", "stories", "stories", "profile", "profile"])
        self.assertEqual([s.key for s in saved], ["21", "22", "31", "32", "profile/profile_11.jpg", "profile/cover_12.png"])
        self.assertEqual(saved[0].post.headers, {"Referer": f"{SITE}/"})
        self.assertEqual([s.file.audio for s in saved], [None] * 6)
        self.assertIn("tagged: this profile has no such tab", seen.messages)
        self.assertIn("photos: 1 file found", seen.messages)

    def test_dash_videos_are_saved_with_their_audio(self):
        story = {"data": {"bucket": {"unified_stories_with_notes": {"edges": [{"node": {
            "creation_time": str(STAMP), "attachments": [{"media": {**with_dash(VIDEO, INSTAGRAM), "id": "31"}}]}}]}}}}
        pages = {f"{SITE}/example.page": embedded(header("reels_tab")), f"{SITE}/stories/900/": embedded(story)}
        scrolls = {f"{SITE}/example.page/reels_tab": [[{"data": {"node": with_dash(VIDEO, INSTAGRAM)}}]]}
        saved = read(pages=pages, scrolls=scrolls, categories=("reels", "stories"))
        self.assertEqual([s.path for s in saved], [f"reels/{DAY}_22.mp4", f"stories/{DAY}_31.mp4"])
        self.assertEqual([s.file.audio for s in saved], ["https://cdn.example/a95.mp4"] * 2)

    def test_each_album_is_opened_into_its_folder(self):
        album_url = f"{SITE}/media/set/?set=a.5&type=3"
        tiles = {"data": {"node": {"edges": [{"node": {"__typename": "TimelineAppCollectionItem", "url": album_url,
                                                       "title": {"text": "Trip"}, "node": {"__typename": "Album"}}}]}}}
        in_album = {"__typename": "Photo", "id": "41", "image": {"uri": "https://cdn.example/41.jpg"}}
        seen = Seen()
        saved = read("fbk:a", seen=seen, categories=("albums",), pages={
            f"{SITE}/a": embedded(header("photos_albums")), f"{SITE}/a/photos_albums": embedded(tiles),
            album_url: embedded({"data": in_album})})
        self.assertEqual(seen.visited, [f"{SITE}/a", f"{SITE}/a/photos_albums", album_url])
        self.assertEqual([s.path for s in saved], ["albums/Trip/41.jpg"])

    def test_without_a_header_every_tab_is_tried(self):
        seen = Seen()
        read("fbk:a", seen=seen, pages={}, categories=("videos",))
        self.assertEqual(seen.visited, [f"{SITE}/a", f"{SITE}/a/videos_by"])
        seen = Seen()
        read("fbk:a", seen=seen, pages={}, categories=("profile", "stories"))
        self.assertIn("! the profile header wasn't found; its pictures are skipped", seen.messages)
        self.assertIn("! the profile header wasn't found; its stories are skipped", seen.messages)

    def test_tagged_is_taken_without_from_and_left_out_by_another_from(self):
        pages = {f"{SITE}/a": embedded(header("photos_of"))}
        for categories, visited in ((None, [f"{SITE}/a", f"{SITE}/a/photos_of", f"{SITE}/stories/900/"]), (("posts",), [f"{SITE}/a"]),
                                    (("tagged",), [f"{SITE}/a", f"{SITE}/a/photos_of"])):
            with self.subTest(categories=categories):
                seen = Seen()
                read("fbk:a", seen=seen, pages=pages, categories=categories)
                self.assertEqual(seen.visited, visited)

    def test_the_posts_tab_reads_the_page_and_the_responses_of_its_first_load(self):
        # Neither is filtered by owner: the header that names the owner came from the same results.
        pages = {f"{SITE}/a": embedded(header(user_id="42"), photo("1", owner="999"))}
        early = {f"{SITE}/a": [photo("2", owner="999")]}
        scrolls = {f"{SITE}/a": [[photo("3", owner="999"), photo("4", owner="42")]]}
        saved = read("fbk:a", pages=pages, early=early, scrolls=scrolls, categories=("posts",))
        self.assertEqual([s.key for s in saved], ["1", "2", "4"])

    def test_dates_are_utc_days(self):
        # 23:59:59 UTC: a local-time day would be the next one east of Greenwich.
        pages = {f"{SITE}/a": embedded(header(), photo("1", stamp=1700006399))}
        [saved] = read("fbk:a", pages=pages, categories=("posts",))
        self.assertEqual(saved.path, "posts/2023-11-14_1.jpg")

    def test_time_keeps_undated_files_and_drops_files_outside_it(self):
        pages = {f"{SITE}/a": embedded(header(), photo("1"), photo("2", stamp=0))}
        saved = read("fbk:a", pages=pages, categories=("posts",), since=datetime.date(2023, 11, 16))
        self.assertEqual([s.key for s in saved], ["2"])

    def test_being_blocked_stops_the_target(self):
        pages = {f"{SITE}/a": "<html><body>You've been temporarily blocked from using this feature</body></html>"}
        with self.assertRaisesRegex(FetchError, "temporarily blocked"):
            read("fbk:a", pages=pages)

    def test_a_login_page_stops_the_target(self):
        with self.assertRaisesRegex(LoginError, "flnd login fbk"):
            read("fbk:a", pages={}, redirects={f"{SITE}/a": f"{SITE}/login/?next=%2Fa"})


if __name__ == "__main__":
    unittest.main()
