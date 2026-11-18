"""Facebook: the routes, the profile header, the tabs and categories, the media walker, stories,
pictures, and the single links (a video, a photo.php, a permalink.php, an fb.watch short link).

What it fakes: the site and the browser. `fakes.collect` runs the routes and categories on a `FakeSite`,
whose tab (`FeedSite`) shows the pages' embedded results, and whose feeds get the responses of a first
load and one batch of responses per scroll. No network and no browser; a `back_off` only records.
"""

import datetime
import json
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from fakes import FakeSite, Seen, collect

from flnd.engine.options import Options
from flnd.source import LoginError, NotFound, Post, SiteError, Target, UsageError
from flnd.sources.fbk import Facebook, image_extension, media_items

SITE = "https://www.facebook.com"
STAMP = 1700000000
DAY = "2023-11-14"
USER = "user"
SINGLE = "this kind of link isn't supported yet"
VIDEO_KIND, PHOTO_KIND, POST_KIND, WATCH_KIND = "video", "photo", "post", "watch"


def representation(id, mime, url, *, height=None, bandwidth=1000):
    """One stream of a DASH manifest, in one file (`SegmentBase`), as Meta serves them."""
    size = f' width="{height * 9 // 16}" height="{height}"' if height else ""
    return (f'<Representation id="{id}" mimeType="{mime}"{size} bandwidth="{bandwidth}">'
            f'<BaseURL>{url}</BaseURL><SegmentBase indexRange="1-2"/></Representation>')


def manifest(videos, audios):
    """A DASH manifest (MPD) with one adaptation set of videos and one of audios."""
    return ('<?xml version="1.0"?>\n<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static">'
            f'<Period><AdaptationSet id="0">{"".join(videos)}</AdaptationSet>'
            f'<AdaptationSet id="1">{"".join(audios)}</AdaptationSet></Period></MPD>')


# Shaped like Meta's: three H.264 heights and two audio bitrates; the tallest and the highest win.
INSTAGRAM = manifest(
    [representation("1v", "video/mp4", "https://cdn.example/360.mp4", height=640, bandwidth=168170),
     representation("2v", "video/mp4", "https://cdn.example/1080.mp4", height=1920,
                    bandwidth=1160975),
     representation("3v", "video/mp4", "https://cdn.example/720.mp4", height=1280,
                    bandwidth=825291)],
    [representation("4a", "audio/mp4", "https://cdn.example/a64.mp4", bandwidth=64353),
     representation("5a", "audio/mp4", "https://cdn.example/a95.mp4", bandwidth=95480)])
# A VP9 and Opus pair: both WebM, so the engine joins them into a .webm.
WEBM = manifest(
    [representation("v", "video/webm", "https://cdn.example/v.webm", height=720)],
    [representation("a", "audio/webm", "https://cdn.example/a.webm")])

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
    ("https://www.facebook.com/profile.php?id=100012345678901", (USER, "100012345678901")),
    # A profile's id is digits (`{user:\d+}`), so a non-numeric id is refused.
    ("https://www.facebook.com/profile.php?id=abc", "can't read"),
    # A name that starts like a page of the site is still a profile.
    ("https://www.facebook.com/watchman", (USER, "watchman")),
    ("https://www.facebook.com/example.page/posts/123", SINGLE),
    ("https://www.facebook.com/example.page/posts", SINGLE),
    # `about` isn't a tab, so it reads the profile. Only the listed pages are refused.
    ("https://www.facebook.com/example.page/about", (USER, "example.page")),
    # A single video, reel or photo by its link.
    ("https://www.facebook.com/watch/?v=123", (VIDEO_KIND, "123")),
    ("fbk:facebook.com/watch/?v=1", (VIDEO_KIND, "1")),
    ("https://fb.com/watch/?v=1", (VIDEO_KIND, "1")),
    ("https://www.facebook.com/example.page/videos/2", (VIDEO_KIND, "2")),
    ("https://fb.com/example.page/videos/2", (VIDEO_KIND, "2")),
    ("https://fb.com/example.page/posts/123", SINGLE),
    ("https://fb.com/reel/1", (VIDEO_KIND, "1")),
    ("https://fb.com/reels/1", (VIDEO_KIND, "1")),
    ("https://fb.com/videos/1", (VIDEO_KIND, "1")),
    ("https://www.facebook.com/reel/123", (VIDEO_KIND, "123")),
    ("https://www.facebook.com/reels/123", (VIDEO_KIND, "123")),
    ("https://www.facebook.com/videos/123", (VIDEO_KIND, "123")),
    ("https://fb.com/photo/?fbid=1", (PHOTO_KIND, "1")),
    ("https://www.facebook.com/photo/?fbid=1", (PHOTO_KIND, "1")),
    ("https://www.facebook.com/photo.php?fbid=123&set=a.4&type=3", (PHOTO_KIND, "123")),
    ("https://m.facebook.com/photo.php?fbid=123", (PHOTO_KIND, "123")),
    ("https://fb.com/photo.php?fbid=123", (PHOTO_KIND, "123")),
    # A permalink names its post by two values: `<owner id>_<story_fbid>`.
    ("https://www.facebook.com/permalink.php?story_fbid=pfbid0Abc&id=100012345678901",
     (POST_KIND, "100012345678901_pfbid0Abc")),
    ("https://www.facebook.com/story.php?story_fbid=7&id=42", (POST_KIND, "42_7")),
    # The fb.watch short link is the `watch` route, followed in the browser.
    ("https://fb.watch/aBc-12/", (WATCH_KIND, "aBc-12")),
    ("fb.watch/aBc-12", (WATCH_KIND, "aBc-12")),
    # The site's own pages are never a profile's name.
    ("https://www.facebook.com/photo.php", "a photo.php link names its photo by fbid="),
    ("https://www.facebook.com/photo.php?fbid=abc", "a photo.php link names its photo by fbid="),
    ("https://www.facebook.com/photo/", "a photo link names its photo by fbid="),
    ("https://www.facebook.com/permalink.php", "a permalink.php link names its post"),
    ("https://www.facebook.com/permalink.php?story_fbid=1", "a permalink.php link names its post"),
    ("https://fb.com/story.php?story_fbid=1", "a story.php link names its post"),
    ("https://www.facebook.com/story.php?id=1", "a story.php link names its post"),
    ("fbk:photo.php", "'photo.php' is a page of Facebook"),
    ("fbk:permalink.php", "'permalink.php' is a page of Facebook"),
    ("https://fb.com/photos/1", SINGLE),
    ("https://fb.com/groups/x", SINGLE),
    ("https://fb.com/events/x", SINGLE),
    ("https://fb.com/stories/x/1", SINGLE),
    ("https://www.facebook.com/photos/abc", SINGLE),
    ("https://www.facebook.com/groups/x", SINGLE),
    ("https://www.facebook.com/events/x", SINGLE),
    ("https://www.facebook.com/stories/x/1", SINGLE),
    ("https://fb.com/profile.php?id=100012345678901", (USER, "100012345678901")),
    ("https://fb.com/people/example/100012345678901", (USER, "100012345678901")),
    ("https://fb.com/@example.page", (USER, "example.page")),
    # `people/<id>` is a profile; a `people` path without an id is refused.
    ("https://www.facebook.com/people/example", "can't read"),
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


# Where the feed's responses come from: what `fbk.FEED` matches.
GRAPHQL = "https://www.facebook.com/api/graphql/"


class FeedSite(FakeSite):
    """A `FakeSite` whose tab delivers `early` when the network is awaited and one batch of `scrolls` per scroll.

    Args:
        scrolls: `{url: [batch, …]}`: each scroll on that page brings the next batch of responses.
        early: `{url: batch}`: what waiting for the network on that page brings, once.
        redirects: `{url: shown}`: where the tab says it is after opening a URL.
        lifted: what a `back_off` changes, as a block lifting would: `{url: page}` replaces those
            pages, and every redirect goes.
    """

    def __init__(self, *args, scrolls=(), early=(), redirects=(), lifted=None, **options):
        super().__init__(*args, **options)
        self.scrolls = {url: list(batches) for url, batches in dict(scrolls).items()}
        self.early = {url: list(batch) for url, batch in dict(early).items()}
        self.redirects = dict(redirects)
        self.lifted = lifted

    @property
    def url(self):
        opened = super().url
        return self.redirects.get(opened, opened)

    def _deliver(self, *, scroll):
        opened = super().url
        if scroll:
            self._seen.scrolls += 1
            batches = self.scrolls.get(opened, [])
            batch = batches.pop(0) if batches else []
        else:
            batch = self.early.pop(opened, [])
        for data in batch:
            for feed in list(self._feeds):
                feed._push(GRAPHQL, data)

    def back_off(self, reason, *, retry_after=None):
        super().back_off(reason, retry_after=retry_after)
        if self.lifted is not None:
            self._pages.update(self.lifted)
            self.redirects.clear()


def read(text="fbk:example.page", *, pages=None, scrolls=None, early=None, redirects=None, seen=None, lifted=None,
         html=None, **options):
    """What the job would download for `text`, through a `FeedSite`; `options` are `Options`'."""
    def site(*args, **kwargs):
        return FeedSite(*args, scrolls=scrolls or {}, early=early or {}, redirects=redirects or {}, lifted=lifted,
                        **kwargs)

    with mock.patch("fakes.FakeSite", site):
        return collect(Facebook, text, pages=PROFILE_PAGES if pages is None else pages, options=Options(**options),
                       seen=seen, html=html)


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
                    kind, found = expected
                    self.assertEqual(Facebook().resolve(value),
                                     Target(f"fbk:{found}", kind=kind))
                else:
                    with self.assertRaisesRegex(UsageError, f"^fbk: {expected}"):
                        Facebook().resolve(value)


class DateHookTest(unittest.TestCase):
    """The photo's own page dates a photo the feed left undated, asked from the tab."""

    def test_the_first_created_time_on_the_photo_page_is_its_day(self):
        url = "https://www.facebook.com/photo/"
        page = '<script>{"created_time":1767312000,"x":{"created_time":1}}</script>'
        site = FakeSite(Target("fbk:example.page", kind="user"), html={url: page})
        self.assertEqual(Facebook().date(site, Post("123")), 1767312000)
        method, asked, options = site._seen.requests[0]
        self.assertEqual((asked, options["in_page"]), (f"{url}?fbid=123", True))

    def test_a_page_without_one_gives_none(self):
        site = FakeSite(Target("fbk:example.page", kind="user"),
                        html={"https://www.facebook.com/photo/": "<p>gone</p>"})
        self.assertIsNone(Facebook().date(site, Post("123")))


class WalkerTest(unittest.TestCase):
    def test_a_dash_manifest_gives_its_best_video_and_audio(self):
        [item] = media_items({"data": {"node": with_dash(VIDEO, INSTAGRAM)}})
        self.assertEqual((item.url, item.audio), ("https://cdn.example/1080.mp4", "https://cdn.example/a95.mp4"))
        [item] = media_items({"data": {"node": with_dash(VIDEO, "<MPD/>")}})
        # Every progressive address is a mirror, HD first (fixes #8).
        self.assertEqual((item.url, item.audio), (("https://cdn.example/hd.mp4", "https://cdn.example/sd.mp4"), None))

    def test_photos_and_videos_are_found_once_with_their_dates(self):
        items = list(media_items({"data": {"a": PHOTO, "b": [VIDEO, VIDEO], "c": {"id": "x", "image": {"w": 1}}}}))
        self.assertEqual([(item.id, item.kind, item.date, item.album) for item in items],
                         [("21", "image", STAMP, "Summer: 2026"), ("22", "video", STAMP, None)])
        self.assertEqual(items[1].url, ("https://cdn.example/hd.mp4", "https://cdn.example/sd.mp4"))

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
        saved = read("fbk:example_user", seen=seen, categories=("albums",), pages={
            f"{SITE}/example_user": embedded(header("photos_albums")), f"{SITE}/example_user/photos_albums": embedded(tiles),
            album_url: embedded({"data": in_album})})
        self.assertEqual(seen.visited, [f"{SITE}/example_user", f"{SITE}/example_user/photos_albums", album_url])
        self.assertEqual([s.path for s in saved], ["albums/Trip/41.jpg"])

    def test_without_a_header_every_tab_is_tried(self):
        seen = Seen()
        read("fbk:example_user", seen=seen, pages={}, categories=("videos",))
        self.assertEqual(seen.visited, [f"{SITE}/example_user", f"{SITE}/example_user/videos_by"])
        seen = Seen()
        read("fbk:example_user", seen=seen, pages={}, categories=("profile", "stories"))
        self.assertIn("! the profile header wasn't found; its pictures are skipped", seen.messages)
        self.assertIn("! the profile header wasn't found; its stories are skipped", seen.messages)

    def test_tagged_is_taken_without_from_and_left_out_by_another_from(self):
        pages = {f"{SITE}/example_user": embedded(header("photos_of"))}
        for categories, visited in ((None, [f"{SITE}/example_user", f"{SITE}/example_user/photos_of", f"{SITE}/stories/900/"]), (("posts",), [f"{SITE}/example_user"]),
                                    (("tagged",), [f"{SITE}/example_user", f"{SITE}/example_user/photos_of"])):
            with self.subTest(categories=categories):
                seen = Seen()
                read("fbk:example_user", seen=seen, pages=pages, categories=categories)
                self.assertEqual(seen.visited, visited)

    def test_the_posts_tab_reads_the_page_and_the_responses_of_its_first_load(self):
        # Neither is filtered by owner: the header that names the owner came from the same results.
        pages = {f"{SITE}/example_user": embedded(header(user_id="42"), photo("1", owner="999"))}
        early = {f"{SITE}/example_user": [photo("2", owner="999")]}
        scrolls = {f"{SITE}/example_user": [[photo("3", owner="999"), photo("4", owner="42")]]}
        saved = read("fbk:example_user", pages=pages, early=early, scrolls=scrolls, categories=("posts",))
        self.assertEqual([s.key for s in saved], ["1", "2", "4"])

    def test_dates_are_utc_days(self):
        # 23:59:59 UTC: a local-time day would be the next one east of Greenwich.
        pages = {f"{SITE}/example_user": embedded(header(), photo("1", stamp=1700006399))}
        [saved] = read("fbk:example_user", pages=pages, categories=("posts",))
        self.assertEqual(saved.path, "posts/2023-11-14_1.jpg")

    def test_time_keeps_undated_files_and_drops_files_outside_it(self):
        pages = {f"{SITE}/example_user": embedded(header(), photo("1"), photo("2", stamp=0))}
        saved = read("fbk:example_user", pages=pages, categories=("posts",), since=datetime.date(2023, 11, 16))
        self.assertEqual([s.key for s in saved], ["2"])

    def test_being_blocked_is_waited_out_and_the_page_loaded_again(self):
        """flows.md #8: a block is seen, never mistaken for an empty tab, and waited out (the user's rule)."""
        pages = {f"{SITE}/example_user": "<html><body>You've been temporarily blocked from using this feature</body></html>"}
        seen = Seen()
        saved = read("fbk:example_user", pages=pages, seen=seen, categories=("posts",),
                     lifted={f"{SITE}/example_user": embedded(header(), photo("1"))})
        self.assertEqual(seen.back_offs, ["Facebook says you've been temporarily blocked"])
        self.assertEqual(seen.visited, [f"{SITE}/example_user"] * 2)
        self.assertEqual([s.key for s in saved], ["1"])

    def test_a_login_page_is_waited_out_and_the_page_loaded_again(self):
        seen = Seen()
        saved = read("fbk:example_user", pages={}, seen=seen, categories=("posts",),
                     redirects={f"{SITE}/example_user": f"{SITE}/login/?next=%2Fa"},
                     lifted={f"{SITE}/example_user": embedded(header(), photo("1"))})
        self.assertEqual(len(seen.back_offs), 1)
        self.assertIn("flnd login fbk", seen.back_offs[0])
        self.assertEqual([s.key for s in saved], ["1"])


class AlternatingSite:
    """A site whose tab shows the given pages in turn, one per load, and counts its back-offs."""

    def __init__(self, *shown):
        self.shown, self.loads, self.back_offs = shown, 0, []

    @property
    def url(self):
        return self.shown[self.loads % len(self.shown)][0]

    def document(self):
        return SimpleNamespace(text=self.shown[self.loads % len(self.shown)][1])

    def back_off(self, reason):
        self.back_offs.append(reason)
        self.loads += 1


class ReadPageTest(unittest.TestCase):
    def test_a_login_wall_and_a_block_in_turn_still_give_up(self):
        from flnd.sources import fbk

        site = AlternatingSite((f"{SITE}/login/", ""), (f"{SITE}/example_user", "temporarily blocked"))
        with mock.patch.object(fbk, "meta_results", return_value=[]):
            with self.assertRaises((LoginError, SiteError)):
                fbk.read_page(cast(Any, site), f"{SITE}/example_user")
        self.assertLessEqual(len(site.back_offs), fbk.WALL_ATTEMPTS + fbk.BLOCK_ATTEMPTS)

    def test_a_profile_named_like_the_login_page_is_no_wall(self):
        from flnd.sources import fbk

        site = AlternatingSite((f"{SITE}/login.example", ""))
        with mock.patch.object(fbk, "meta_results", return_value=[]):
            self.assertEqual(fbk.read_page(cast(Any, site), f"{SITE}/login.example"), [])
        self.assertEqual(site.back_offs, [])

def info_line(cursor):
    """A streamed line with the timeline's `page_info`; "" for the last page."""
    return {"label": "ProfileCometTimelineFeed_user$stream$timeline_list_feed_units",
            "data": {"page_info": {"has_next_page": bool(cursor), "end_cursor": cursor or None}}}


class ScrollTest(unittest.TestCase):
    def test_multi_batch_scroll_collects_all_batches_before_yielding(self):
        """2026-11, live: Tiles `has_next_page` false comes before real timeline batches; all scroll first."""
        seen = Seen()
        pages = {f"{SITE}/example_user": embedded(header(user_id="42"), photo("1", owner="42"))}
        scrolls = {f"{SITE}/example_user": [
            [photo("2", owner="42"), info_line("")],
            [photo("3", owner="42"), info_line("c3")],
            [photo("4", owner="42"), info_line("")],
        ]}
        saved = read("fbk:example_user", pages=pages, scrolls=scrolls, seen=seen, categories=("posts",))
        self.assertEqual([s.key for s in saved], ["1", "2", "3", "4"])
        self.assertEqual([s.path for s in saved], [f"posts/2023-11-15_{n}.jpg" for n in "1234"])

    def test_block_during_scroll_backs_off_and_continues(self):
        seen = Seen()
        pages = {f"{SITE}/example_user": embedded(header(user_id="42"), photo("1", owner="42"))}
        scrolls = {f"{SITE}/example_user": [
            [photo("2", owner="42")],
            [{"__dr": "CometErrorRoot.react"}],
            [photo("3", owner="42")],
        ]}
        saved = read("fbk:example_user", pages=pages, scrolls=scrolls, seen=seen, categories=("posts",))
        self.assertEqual(seen.back_offs, ["Facebook says you've been temporarily blocked"])
        self.assertEqual([s.key for s in saved], ["1", "2", "3"])

    def test_persistent_block_during_scroll_raises_fetch_error(self):
        seen = Seen()
        pages = {f"{SITE}/example_user": embedded(header(user_id="42"), photo("1", owner="42"))}
        scrolls = {f"{SITE}/example_user": [[{"__dr": "CometErrorRoot.react"}]] * 4}
        with self.assertRaisesRegex(SiteError, "temporarily blocked"):
            read("fbk:example_user", pages=pages, scrolls=scrolls, seen=seen, categories=("posts",))
        self.assertEqual(len(seen.back_offs), 3)

    def test_a_comment_lists_page_info_is_not_the_tabs(self):
        from flnd.sources.fbk import page_info
        comment = {"data": {"node": {"story": {"feedback": {"comments": {"page_info": {"has_next_page": True,
                                                                                         "end_cursor": "x"}}}}}}}
        self.assertIsNone(page_info(comment))
        self.assertEqual(page_info(info_line("c9")), (True, "c9"))
        self.assertEqual(page_info({"data": {"node": {"timeline_list_feed_units": {
            "page_info": {"has_next_page": False, "end_cursor": "z"}}}}}), (False, "z"))

    def test_persistent_login_page_raises_login_error(self):
        seen = Seen()
        with self.assertRaisesRegex(LoginError, "fbk: Facebook kept asking for a login") as caught:
            read("fbk:example_user", pages={}, seen=seen, categories=("posts",),
                 redirects={f"{SITE}/example_user": f"{SITE}/login/?next=%2Fa"})
        self.assertIn("flnd login fbk", caught.exception.hint)
        self.assertEqual(len(seen.back_offs), 2)


class SingleLinkTest(unittest.TestCase):
    """A video, photo, post or fb.watch link: its one page, its media, in the target's folder."""

    def test_a_video_link_keeps_only_the_video_it_names(self):
        url = f"{SITE}/watch/?v=22"
        seen = Seen()
        saved = read(f"{SITE}/example.page/videos/22/", seen=seen,
                     pages={url: embedded(video("22"), video("99"))})
        self.assertEqual(seen.visited, [url])
        self.assertEqual([(s.path, s.key) for s in saved], [("2023-11-15_22.mp4", "22")])
        self.assertEqual(saved[0].post.category, "")

    def test_a_fb_watch_link_is_followed_to_the_video_it_points_to(self):
        # Bug: fb.watch links matched no route. The short link redirects in the browser, and the
        # video's id is read from where the tab lands.
        short = "https://fb.watch/aBc-12/"
        seen = Seen()
        saved = read(short, seen=seen, redirects={short: f"{SITE}/reel/22"},
                     pages={f"{SITE}/watch/?v=22": embedded(video("22"), video("99"))})
        self.assertEqual(seen.visited, [short, f"{SITE}/watch/?v=22"])
        # Keyed by the video's id, so the same video by its long link isn't saved twice.
        self.assertEqual([(s.path, s.key) for s in saved], [("2023-11-15_22.mp4", "22")])

    def test_a_fb_watch_link_to_a_page_videos_url_reads_its_id(self):
        short = "https://fb.watch/aBc-12/"
        landed = f"{SITE}/example.page/videos/some-title/22/"
        saved = read(short, redirects={short: landed},
                     pages={f"{SITE}/watch/?v=22": embedded(video("22"))})
        self.assertEqual([s.key for s in saved], ["22"])

    def test_a_fb_watch_link_that_leads_nowhere_is_refused_clearly(self):
        short = "https://fb.watch/aBc-12/"
        with self.assertRaisesRegex(NotFound, "didn't lead to a video"):
            read(short, pages={}, redirects={short: f"{SITE}/example.page"})
        with self.assertRaisesRegex(LoginError, "login page") as caught:
            read(short, pages={}, redirects={short: f"{SITE}/login/?next=x"})
        self.assertIn("flnd login fbk", caught.exception.hint)

    def test_a_photo_php_link_is_the_photo_not_a_user(self):
        # Bug: photo.php was read as the user "photo.php".
        url = f"{SITE}/photo/?fbid=21"
        seen = Seen()
        saved = read(f"{SITE}/photo.php?fbid=21&set=a.4", seen=seen,
                     pages={url: embedded(photo("21"), photo("5"))})
        self.assertEqual(seen.visited, [url])
        self.assertEqual([(s.path, s.key) for s in saved], [("2023-11-15_21.jpg", "21")])

    def test_a_permalink_is_the_posts_media_its_owner_owns(self):
        # Bug: permalink.php was read as the user "permalink.php".
        url = f"{SITE}/permalink.php?story_fbid=7&id=42"
        seen = Seen()
        saved = read(f"{SITE}/permalink.php?story_fbid=7&id=42", seen=seen, pages={
            url: embedded(photo("1", owner="42"), photo("2", owner="999"), video("3"))})
        self.assertEqual(seen.visited, [url])
        self.assertEqual([s.key for s in saved], ["1", "3"])

    def test_a_page_without_the_media_is_not_found(self):
        with self.assertRaisesRegex(NotFound, "shows no photo or video"):
            read(f"{SITE}/photo.php?fbid=21", pages={f"{SITE}/photo/?fbid=21": embedded(photo("5"))})


class DashNameTest(unittest.TestCase):
    """A DASH video is a `File(video, audio=…)` with no name: the engine names it from the templates."""

    def test_a_dash_video_is_named_by_the_templates_as_before(self):
        pages = {f"{SITE}/watch/?v=22": embedded(video("22", manifest=INSTAGRAM)),
                 f"{SITE}/watch/?v=23": embedded(video("23", stamp=0, manifest=INSTAGRAM))}
        [dated] = read(f"{SITE}/watch/?v=22", pages=pages)
        [undated] = read(f"{SITE}/watch/?v=23", pages=pages)
        self.assertIsNone(dated.file.name)
        self.assertEqual((dated.file.url, dated.file.audio),
                         ("https://cdn.example/1080.mp4", "https://cdn.example/a95.mp4"))
        self.assertEqual([(dated.path, dated.key), (undated.path, undated.key)],
                         [("2023-11-15_22.mp4", "22"), ("23.mp4", "23")])

    def test_a_webm_pair_is_joined_into_webm(self):
        pages = {f"{SITE}/watch/?v=22": embedded(video("22", manifest=WEBM))}
        [saved] = read(f"{SITE}/watch/?v=22", pages=pages)
        self.assertEqual((saved.path, saved.key), ("2023-11-15_22.webm", "22"))


if __name__ == "__main__":
    unittest.main()
