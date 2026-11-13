"""TikTok: its routes, the names and keys of every file it saves, the captcha, the tabs, and one video from its embed page.

What is faked: the site's answers (`FakeSite` through `collect`): the profile's HTML and its list APIs,
asked for directly, and the browser's tab, by `FakeTab`, which hands the profile's JSON responses to
whoever watches for them when the source scrolls, the fallback. No network, no browser, no wait is slept.
"""

import json
import os
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from fakes import FakeSite, Seen, collect

from flnd.session import Options
from flnd.source import FetchError, Target, TargetError
from flnd.sources.tik import TIKTOK_UA, TikTok, best_video
from shared import selector
from shared._browser import Subscription

DAY = datetime.fromtimestamp(1700000000, timezone.utc).strftime("%Y-%m-%d")
PROFILE = "https://www.tiktok.com/@someone"
VIDEO = "7301234567890123456"

# Every form tik reads, as people write it, and what it becomes: (kind, value), or a refusal's text.
# The kinds and values are the keys of targets.db and the history, so a change here is a migration.
ROUTES = [
    ("tik:example_creator", ("user", "example_creator")),
    ("tik:@example_creator", ("user", "example_creator")),
    ("example_creator", ("user", "example_creator")),
    ("tik:example.user", ("user", "example.user")),
    # A number of 15 digits or fewer is a name; 16 or more is a video id.
    ("tik:123456789012345", ("user", "123456789012345")),
    ("https://www.tiktok.com/@example_creator", ("user", "example_creator")),
    ("https://www.tiktok.com/@example.creator", ("user", "example.creator")),
    ("https://www.tiktok.com/@example_creator?lang=en", ("user", "example_creator")),
    ("tiktok.com/@example_creator", ("user", "example_creator")),
    ("https://m.tiktok.com/@example_creator/", ("user", "example_creator")),
    ("https://www.tiktok.com/@example_creator#top", ("user", "example_creator")),
    # A profile's tab is the profile, and so is a path that isn't a video.
    ("https://www.tiktok.com/@example_creator/liked", ("user", "example_creator")),
    ("https://www.tiktok.com/@example_creator/video/", ("user", "example_creator")),
    ("https://www.tiktok.com/@example_creator/video/abc", ("user", "example_creator")),
    ("https://www.tiktok.com/@example_creator/video/7301234567890123456x", ("user", "example_creator")),
    ("https://www.tiktok.com/@example_creator/x/7301234567890123456", ("user", "example_creator")),
    ("tik:7301234567890123456", ("video", VIDEO)),
    ("tik:1234567890123456", ("video", "1234567890123456")),
    ("https://www.tiktok.com/@example_creator/video/7301234567890123456", ("video", VIDEO)),
    ("https://www.tiktok.com/@example_creator/photo/7301234567890123456", ("video", VIDEO)),
    ("https://www.tiktok.com/@example_creator/video/7301234567890123456/?lang=en", ("video", VIDEO)),
    ("https://www.tiktok.com/@example_creator/video/7301234567890123456/extra", ("video", VIDEO)),
    ("https://www.tiktok.com/@example_creator/video/7301234567890123456/photo/1", ("video", VIDEO)),
    ("https://www.tiktok.com/@example_creator/video/123", ("video", "123")),
    ("https://www.tiktok.com/@example_creator/video/0", ("video", "0")),
    # Without a user, as the old reader took them too.
    ("https://www.tiktok.com/video/7301234567890123456", ("video", VIDEO)),
    ("https://www.tiktok.com/photo/7301234567890123456", ("video", VIDEO)),
    ("tik:not a name", "can't read"),
    ("tik:ex-ample", "can't read"),
    ("tik:@@example_creator", "can't read"),
    ("tik:", "can't read"),
    ("tik:https://www.tiktok.com/", "can't read"),
    ("https://www.tiktok.com/", "can't read"),
    ("https://www.tiktok.com/example_creator", "can't read"),
    ("https://www.tiktok.com/@my-user", "can't read"),
    ("https://www.tiktok.com/@", "can't read"),
    ("https://www.tiktok.com/@@example_creator", "can't read"),
    ("https://www.tiktok.com/video/", "can't read"),
    # A short link is no route's form: it's refused here, and the engine follows it once.
    ("https://vm.tiktok.com/ZMabc123/", "can't read"),
    ("notiktok.com/@example_creator", "can't read"),
    ("tiktok.com.evil.example/@x", "can't read"),
]


class RoutesTest(unittest.TestCase):
    def test_every_form(self):
        for text, expected in ROUTES:
            with self.subTest(text=text):
                value = text.removeprefix("tik:")
                if isinstance(expected, tuple):
                    self.assertEqual(TikTok().resolve(value), Target("tik", *expected))
                else:
                    with self.assertRaisesRegex(TargetError, f"^tik: {expected}"):
                        TikTok().resolve(value)


CDN = "https://cdn.example"
API = "https://www.tiktok.com"


def video(video_id, url, **extra):
    """One video as the web API gives it."""
    return {"id": video_id, "video": {"playAddr": url}, **extra}


def photo(video_id, *urls, **extra):
    """One photo-mode post as the web API gives it, one picture per URL."""
    return {"id": video_id, "imagePost": {"images": [{"imageURL": {"urlList": [url]}} for url in urls]}, **extra}


def embed_page(item):
    data = {"props": {"pageProps": {"itemStruct": item}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def profile_page(*tabs, video_count=2, sec_uid=None):
    user = {"avatarLarger": "https://p16.example/tos-avt/abc123~tplv-cropcenter:1080:1080.jpeg?x=1"}
    if sec_uid:
        user.update(secUid=sec_uid, id="6800000000000000001")
    data = {"__DEFAULT_SCOPE__": {"webapp.user-detail": {"userInfo": {
        "user": user, "stats": {"videoCount": video_count}}}}}
    tab_html = "".join(f'<p data-e2e="{tab}">{tab}</p>' for tab in tabs)
    return (f'<html><body>{tab_html}<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
            f"{json.dumps(data)}</script></body></html>")


class FakeTab:
    """The browser's tab as `watch_responses` and the source use it: responses are delivered when the page scrolls.

    Args:
        seen: what the `FakeSite` records, which `html` may read (the back-offs, the pages loaded).
        feeds: (URL, JSON) responses the site would send, in order. One goes to every watcher whose
            match takes its URL, on the first scroll or wait after that watcher started; one nobody
            watches for stays until somebody does, as a tab's list comes only once it is clicked.
        html: the page's HTML, or a function of the tab giving it.
    """

    def __init__(self, seen, feeds=(), html=""):
        self.seen = seen
        self.pending = list(feeds)
        self.html = html
        self.handlers = []
        self.scrolls = 0
        self.raw = mock.MagicMock()

    @property
    def document(self):
        return selector.parse(self.html(self) if callable(self.html) else self.html)

    def on_response(self, match, handler, *, lines=False):
        matches = (lambda url: match in url) if isinstance(match, str) else match
        entry = (matches, handler)
        self.handlers.append(entry)
        return Subscription(lambda: self.handlers.remove(entry) if entry in self.handlers else None)

    def deliver(self):
        for url, data in list(self.pending):
            handlers = [handler for matches, handler in self.handlers if matches(url)]
            if handlers:
                self.pending.remove((url, data))
                for handler in handlers:
                    handler(data, None)

    def scroll(self, *, until=None, **options):
        self.scrolls += 1
        self.deliver()
        return 1

    def wait(self, css=None, *, idle=False, timeout=30.0):
        self.deliver()


def run(text, *, feeds=(), html="", pages=None, json=None, options=Options(), cookies=None):
    """What the job would download for `text`, with `FakeTab` as the browser: (saved files, seen, tab)."""
    seen = Seen()
    tab = FakeTab(seen, feeds, html)
    with mock.patch.object(FakeSite, "page", property(lambda site: tab)):
        saved = collect(TikTok, text, html=pages, json=json, options=options, seen=seen, cookies=cookies)
    return saved, seen, tab


# What the profile `someone` answers with while it scrolls, in the order the old source got them: the
# post feed, the two story feeds, then each tab's. Ids that name a date by themselves (createTime 0,
# or none), a playlist with a `/` and a `:`, a carousel, and a video repeated in the reposts.
POSTS = {"hasMore": False, "itemList": [
    video("7300000000000000001", f"{CDN}/v1", createTime=1700000000),
    video("7300000000000000002", f"{CDN}/v2", createTime=1700000001, playlist={"name": "Road Trip"}),
    photo("7300000000000000003", f"{CDN}/p1a.jpeg", f"{CDN}/p1b", createTime="1700000500"),
    photo("7300000000000000004", f"{CDN}/p2a", f"{CDN}/p2b.jpg", createTime=1700000600, playlist={"name": "A/B"}),
    video("7300000000000000005", f"{CDN}/v3", createTime=0),
    video("5", f"{CDN}/v4"),
    {"id": "7300000000000000006", "createTime": 1700000700, "video": {"bitrateInfo": [
        {"Bitrate": 9, "PlayAddr": {"Width": 1, "Height": 1, "UrlList": [f"{CDN}/best"]}}]}},
    {"id": "7300000000000000007", "createTime": 1700000800},
]}
STORIES = {"storyList": [video("7310000000000000001", f"{CDN}/s1", createTime=1700001000),
                         photo("7310000000000000002", f"{CDN}/s2.jpeg", createTime=1700001001)]}
STORIES_2 = {"story_list": [{"aweme_id": "7310000000000000003", "create_time": 1700001002,
                             "video": {"playAddr": f"{CDN}/s3"}}]}
REPOSTS = {"itemList": [video("7300000000000000001", f"{CDN}/v1", createTime=1700000000),
                        video("7320000000000000001", f"{CDN}/r1", createTime=1700002000, playlist={"name": "Ignored"}),
                        photo("7320000000000000002", f"{CDN}/r2a.jpeg", f"{CDN}/r2b", createTime=1700002001)]}
FAVORITES = {"itemList": [video("7330000000000000001", f"{CDN}/f1", createTime=1700003000)]}
LIKED = {"itemList": [photo("7340000000000000001", f"{CDN}/l1a", createTime=1700004000)]}
FEEDS = [(f"{API}/api/post/item_list/?count=30", POSTS), (f"{API}/api/story/item_list/?id=1", STORIES),
         (f"{API}/api/user/story/?id=1", STORIES_2), (f"{API}/api/repost/item_list/?count=30", REPOSTS),
         (f"{API}/api/user/collect/item_list/?count=30", FAVORITES), (f"{API}/api/favorite/item_list/?count=30", LIKED)]
TABS = profile_page("repost-tab", "favorites-tab", "liked-tab", video_count=8)

# Single posts from the embed page, and what the source they were written for (`run(session)`, before
# step 6) saved for each: (path, history key), recorded by running that source on the same data.
EMBEDS = {
    "7234567890123456789": video("7234567890123456789", f"{CDN}/e1", createTime=1700000000),
    "7234567890123456790": photo("7234567890123456790", f"{CDN}/e2a.jpeg", f"{CDN}/e2b"),
    "7234567890123456791": video("7234567890123456791", f"{CDN}/e3.mp4", createTime="1700000100",
                                 playlist={"name": "Trip/2: A"}),
}
EMBED_PAIRS = {
    "7234567890123456789": [("videos/2023-11-14_7234567890123456789.mp4", "7234567890123456789")],
    "7234567890123456790": [("images/2023-05-18_7234567890123456790_1.jpg", "7234567890123456790_1"),
                            ("images/2023-05-18_7234567890123456790_2.jpg", "7234567890123456790_2")],
    "7234567890123456791": [("albums/Trip_2_A/2023-11-14_7234567890123456791.mp4", "7234567890123456791")],
}
PROFILE_PAIRS = [
    ("profile/avatar_abc123.jpg", "profile/avatar_abc123.jpg"),
    ("videos/2023-11-14_7300000000000000001.mp4", "7300000000000000001"),
    ("albums/Road_Trip/2023-11-14_7300000000000000002.mp4", "7300000000000000002"),
    ("images/2023-11-14_7300000000000000003_1.jpg", "7300000000000000003_1"),
    ("images/2023-11-14_7300000000000000003_2.jpg", "7300000000000000003_2"),
    ("albums/A_B/2023-11-14_7300000000000000004_1.jpg", "7300000000000000004_1"),
    ("albums/A_B/2023-11-14_7300000000000000004_2.jpg", "7300000000000000004_2"),
    ("videos/2023-11-11_7300000000000000005.mp4", "7300000000000000005"),
    ("videos/5.mp4", "5"),
    ("videos/2023-11-14_7300000000000000006.mp4", "7300000000000000006"),
    ("stories/2023-11-14_7310000000000000001.mp4", "7310000000000000001"),
    ("stories/2023-11-14_7310000000000000002_1.jpg", "7310000000000000002_1"),
    ("stories/2023-11-14_7310000000000000003.mp4", "7310000000000000003"),
    ("reposts/2023-11-14_7320000000000000001.mp4", "7320000000000000001"),
    ("reposts/2023-11-14_7320000000000000002_1.jpg", "7320000000000000002_1"),
    ("reposts/2023-11-14_7320000000000000002_2.jpg", "7320000000000000002_2"),
    ("favorites/2023-11-14_7330000000000000001.mp4", "7330000000000000001"),
    ("liked/2023-11-14_7340000000000000001_1.jpg", "7340000000000000001_1"),
]


def pairs(saved):
    return [(item.path, item.key) for item in saved]


class NamesAndKeysTest(unittest.TestCase):
    """Phase 6 rule: no history migrations, so every name and key is what `run(session)` gave for the same data."""

    def test_a_single_post_from_the_embed_page(self):
        for video_id, item in EMBEDS.items():
            with self.subTest(video_id=video_id):
                saved, _, _ = run(f"tik:{video_id}", pages={f"{API}/embed/v2/{video_id}": embed_page(item)})
                self.assertEqual(pairs(saved), EMBED_PAIRS[video_id])

    def test_a_profile_with_every_category_and_its_tabs(self):
        saved, _, _ = run("tik:someone", feeds=FEEDS, html=TABS, options=Options(categories=("all",)))
        # The old source saved the tabs in another order (favorites before liked); no id is in both.
        self.assertEqual(sorted(pairs(saved)), sorted(PROFILE_PAIRS))
        self.assertEqual(len({key for _, key in pairs(saved)}), len(PROFILE_PAIRS))

    def test_the_formats_decide_the_dates_in_utc(self):
        """Fix #15: 2026-01-01 23:30 UTC is 2026-01-01 on every machine, not 2026-01-02 in Kiritimati (UTC+14)."""
        late = int(datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc).timestamp())
        early = int(datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc).timestamp())
        feeds = [(f"{API}/api/post/item_list/", {"hasMore": False, "itemList": [
            video("1", f"{CDN}/a", createTime=late), video("2", f"{CDN}/b", createTime=str(early)),
            video(str(late << 32), f"{CDN}/c")]})]
        for zone in ("Pacific/Kiritimati", "America/Los_Angeles"):
            with self.subTest(zone=zone), far_off_timezone(zone):
                saved, _, _ = run("tik:someone", feeds=feeds, html=profile_page(), options=Options(categories=("videos",)))
                self.assertEqual([item.path for item in saved], [
                    "videos/2026-01-01_1.mp4", "videos/2026-01-01_2.mp4", f"videos/2026-01-01_{late << 32}.mp4"])


class TikTokTest(unittest.TestCase):
    def test_single_video_from_the_embed_page(self):
        video_id = "7234567890123456789"
        item = video(video_id, "https://v.example/clip.mp4", createTime=1700000000)
        saved, seen, _ = run(f"tik:{video_id}", pages={f"{API}/embed/v2/{video_id}": embed_page(item)},
                             cookies={"sessionid": "s", "tt_chain_token": "t"})
        self.assertEqual([item.path for item in saved], [f"videos/{DAY}_{video_id}.mp4"])
        self.assertEqual(saved[0].post.headers, {"Referer": "https://www.tiktok.com/", "User-Agent": TIKTOK_UA,
                                                 "Cookie": "tt_chain_token=t; sessionid=s"})
        self.assertEqual(seen.requests[0][2]["timeout"], 20.0)
        self.assertEqual(seen.requests[0][2]["follow_redirects"], False)

    def test_a_refused_embed_is_a_warning(self):
        saved, seen, _ = run("tik:7234567890123456789")
        self.assertEqual(saved, [])
        self.assertTrue(seen.messages[0].startswith("! HTTP 404"))

    def test_a_playlist_item_belongs_to_playlists_and_not_to_videos(self):
        for category, albums in (("videos", False), ("playlists", True)):
            with self.subTest(category=category):
                saved, _, _ = run("tik:someone", feeds=FEEDS[:1], html=profile_page(),
                                  options=Options(categories=(category,)))
                self.assertEqual([item.path.startswith("albums/") for item in saved], [albums] * len(saved))
                self.assertEqual(len(saved), 3 if albums else 6)

    def test_the_posts_are_filed_by_categories_in_the_declared_order(self):
        self.assertEqual(TikTok.categories, ("videos", "stories", "playlists", "reposts", "liked", "favorites", "profile"))
        self.assertEqual(TikTok.single_kinds, ("video",))

    def test_an_unsolved_captcha_backs_off_and_reloads_until_it_is_gone(self):
        captcha = "<body><div id='captcha_container'>Drag the slider</div></body>"
        saved, seen, _ = run("tik:someone", html=lambda tab: captcha if len(tab.seen.back_offs) < 2 else "<body><p>feed</p></body>")
        self.assertEqual(len(seen.back_offs), 2)
        self.assertEqual(seen.visited, [PROFILE] * 3)

    def test_a_captcha_over_the_grid_is_waited_out_and_the_grid_read_again(self):
        feed = [(f"{API}/api/post/item_list/?count=30",
                 {"hasMore": False, "itemList": [video("7300000000000000001", f"{CDN}/1.mp4", createTime=1700000000)]})]

        class Walled(FakeTab):
            """The grid is covered once the page has scrolled, and the feed comes only after the back-off."""

            def deliver(self):
                if self.seen.back_offs:
                    super().deliver()

        seen = Seen()
        tab = Walled(seen, feed, lambda tab: "<body><div id='captcha_container'>Drag the slider</div></body>"
                     if tab.scrolls and not tab.seen.back_offs else "<body></body>")
        with mock.patch.object(FakeSite, "page", property(lambda site: tab)):
            saved = collect(TikTok, "tik:someone", seen=seen, options=Options(categories=("videos",)))
        self.assertEqual(len(seen.back_offs), 1)
        self.assertEqual([item.path for item in saved], [f"videos/{DAY}_7300000000000000001.mp4"])

    def test_captcha_words_in_scripts_are_not_a_captcha(self):
        _, seen, _ = run("tik:someone", html="<body><script>var t = 'Verify to continue';</script><p>feed</p></body>")
        self.assertEqual(seen.back_offs, [])

    def test_walled_api_falls_back_to_page_links(self):
        video_id = "7234567890123456789"
        item = video(video_id, "https://v.example/clip.mp4", createTime=1700000000)
        saved, _, _ = run("tik:someone", html=f'<body><a href="/@someone/video/{video_id}">clip</a></body>',
                          pages={f"{API}/embed/v2/{video_id}": embed_page(item)},
                          options=Options(categories=("videos",)))
        self.assertEqual([item.path for item in saved], [f"videos/{DAY}_{video_id}.mp4"])

    def test_profile_picture_and_tabs(self):
        one = {"hasMore": False, "itemList": [video("7300000000000000001", f"{CDN}/1.mp4", createTime=1700000000)]}
        feeds = [(f"{API}/api/post/item_list/?count=30", one), FEEDS[3]]
        saved, seen, _ = run("tik:someone", feeds=feeds, html=profile_page("repost-tab", "liked-tab"),
                             options=Options(categories=("all",)))
        self.assertEqual([item.post.category for item in saved], ["videos", "reposts", "reposts", "reposts", "profile"])
        self.assertEqual(saved[-1].path, "profile/avatar_abc123.jpg")
        self.assertIn("videos: 1 found; the profile lists 2", seen.messages)
        self.assertIn("favorites: not public on this account", seen.messages)
        self.assertIn("liked: nothing public on this account", seen.messages)

    def test_tabs_not_asked_for_are_not_read(self):
        _, seen, _ = run("tik:someone", html=profile_page("repost-tab"), options=Options(categories=("videos",)))
        self.assertFalse(any("public" in message for message in seen.messages))

    def test_the_profile_is_opened_once_for_every_category(self):
        _, seen, _ = run("tik:someone", feeds=FEEDS, html=TABS, options=Options(categories=("all",)))
        self.assertEqual(seen.visited, [PROFILE])

    def test_the_best_video_address(self):
        self.assertEqual(best_video({"playAddr": {"urlList": ["https://v/a.mp4"]}}), "https://v/a.mp4")
        # Every CDN address is a mirror, in TikTok's order (fixes #8).
        self.assertEqual(best_video({"bitrateInfo": [{"Bitrate": 9, "PlayAddr": {"UrlList": ["https://v16/a.mp4",
                                                                                         "https://v19/a.mp4"]}}]}),
                         ("https://v16/a.mp4", "https://v19/a.mp4"))
        self.assertEqual(best_video({"playAddr": {"urlList": ["https://v16/a.mp4", "https://v19/a.mp4"]}}),
                         ("https://v16/a.mp4", "https://v19/a.mp4"))
        self.assertEqual(best_video({"bitrateInfo": [{"Bitrate": 1, "PlayAddr": {"UrlList": ["https://v/low.mp4"]}},
                                                     {"Bitrate": 9, "PlayAddr": {"UrlList": ["https://v/high.mp4"]}}]}),
                         "https://v/high.mp4")
        self.assertIsNone(best_video({}))

    def test_best_video_is_the_largest_bitrate_file(self):
        """Fix #16: the largest file of bitrateInfo wins over playAddr, by pixels and then by bitrate."""
        video = {"playAddr": "https://v/play.mp4", "downloadAddr": "https://v/watermarked.mp4", "bitrateInfo": [
            rendition("https://v/720.mp4", 720, 1280, 2_000_000),
            rendition("https://v/1080-low.mp4", 1080, 1920, 1_000_000),
            rendition("https://v/1080-high.mp4", 1080, 1920, 3_000_000),
            rendition("https://v/540.mp4", 540, 960, 9_000_000),
        ]}
        self.assertEqual(best_video(video), "https://v/1080-high.mp4")

    def test_best_video_skips_dash(self):
        for dash in ({"Format": "dash"}, {"GearName": "adapt_1080_dash"}, {"UrlKey": "v0_bytevc1_1080p_dash"},
                     {"manifest": "https://v/1080/manifest.mpd"}):
            with self.subTest(dash=dash):
                marks = dict(dash)
                url = marks.pop("manifest", "https://v/1080.mp4")
                video = {"bitrateInfo": [rendition("https://v/720.mp4", 720, 1280, 1),
                                         rendition(url, 1080, 1920, 5, **marks)]}
                self.assertEqual(best_video(video), "https://v/720.mp4")

    def test_best_video_falls_back_to_play_addr_never_download_addr(self):
        dash_only = [rendition("https://v/1080.mp4", 1080, 1920, 1, Format="dash")]
        self.assertEqual(best_video({"playAddr": "https://v/play.mp4", "downloadAddr": "https://v/wm.mp4",
                                     "bitrateInfo": dash_only}), "https://v/play.mp4")
        self.assertEqual(best_video({"playAddr": {"urlList": ["https://v/play.mp4"]},
                                     "bitrateInfo": [{"Bitrate": 1, "PlayAddr": {"UrlList": []}}]}),
                         "https://v/play.mp4")
        # The watermarked copy is never saved, even when it is all there is.
        self.assertIsNone(best_video({"downloadAddr": "https://v/wm.mp4"}))
        self.assertIsNone(best_video({"downloadAddr": {"urlList": ["https://v/wm.mp4"]}, "playAddr": ""}))


def rendition(url, width, height, bitrate, **extra):
    """One bitrateInfo entry as TikTok's web API gives it; `extra` sets Format or GearName, or UrlKey."""
    entry = {"Bitrate": bitrate, "GearName": f"normal_{width}_0", "Format": "mp4",
             "PlayAddr": {"Width": width, "Height": height, "UrlKey": f"v0_h264_{width}p", "UrlList": [url]}}
    if "UrlKey" in extra:
        entry["PlayAddr"]["UrlKey"] = extra.pop("UrlKey")
    return {**entry, **extra}


class far_off_timezone:
    """Sets the process's local timezone with TZ and tzset, where the platform has them (not Windows)."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        if not hasattr(time, "tzset"):
            raise unittest.SkipTest("no time.tzset on this platform")
        self.previous = os.environ.get("TZ")
        os.environ["TZ"] = self.name
        time.tzset()

    def __exit__(self, *exc_info):
        if self.previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.previous
        time.tzset()

SEC_UID = "MS4wLjABAAAAexample"
# The profile's HTML as flnd's own client gets it: its data with the secUid the list APIs take.
DIRECT_HTML = {PROFILE: profile_page(video_count=8, sec_uid=SEC_UID)}
# Each list API's answers, as the page's scrolling gets them: the same data, asked for directly.
LISTS = {f"{API}/api/post/item_list/": POSTS, f"{API}/api/story/item_list/": {**STORIES, **STORIES_2},
         f"{API}/api/repost/item_list/": REPOSTS, f"{API}/api/user/collect/item_list/": FAVORITES,
         f"{API}/api/favorite/item_list/": LIKED}


class ListApiTest(unittest.TestCase):
    """simplify.md §4: the list APIs directly first, so a profile run needs no page; scrolling only as the fallback."""

    def test_a_profile_is_read_without_the_page_and_named_and_keyed_as_before(self):
        stories = {"storyList": STORIES["storyList"] + [{"id": "7310000000000000003", "createTime": 1700001002,
                                                          "video": {"playAddr": f"{CDN}/s3"}}]}
        saved, seen, tab = run("tik:someone", pages=DIRECT_HTML, json={**LISTS, f"{API}/api/story/item_list/": stories},
                               options=Options(categories=("all",)))
        self.assertEqual(sorted(pairs(saved)), sorted(PROFILE_PAIRS))
        self.assertEqual(seen.visited, [])
        self.assertEqual(seen.back_offs, [])

    def test_each_list_is_asked_with_the_web_clients_parameters(self):
        _, seen, _ = run("tik:someone", pages=DIRECT_HTML, json=LISTS, options=Options(categories=("videos", "stories")))
        lists = [(url, options["params"]) for _, url, options in seen.requests if "/api/" in url]
        self.assertEqual([url for url, _ in lists], [f"{API}/api/post/item_list/", f"{API}/api/story/item_list/"])
        posts, stories = (params for _, params in lists)
        self.assertEqual((posts["secUid"], posts["cursor"], posts["aid"], posts["app_name"]),
                         (SEC_UID, "0", "1988", "tiktok_web"))
        self.assertEqual(stories["authorId"], "6800000000000000001")
        self.assertRegex(posts["device_id"], r"^\d{19}$")

    def test_pages_follow_the_cursor_and_a_repeated_page_gets_a_new_device(self):
        first = {"hasMore": True, "cursor": "1700000500000",
                 "itemList": [video("7300000000000000001", f"{CDN}/v1", createTime=1700000000)]}
        last = {"hasMore": False, "cursor": "0",
                "itemList": [video("7300000000000000002", f"{CDN}/v2", createTime=1700000001)]}
        answers = [first, first, last]
        saved, seen, _ = run("tik:someone", pages=DIRECT_HTML, options=Options(categories=("videos",)),
                             json={f"{API}/api/post/item_list/": lambda url, request: answers.pop(0)})
        params = [options["params"] for _, url, options in seen.requests if "/api/" in url]
        self.assertEqual([p["cursor"] for p in params], ["0", "1700000500000", "1700000500000"])
        self.assertEqual(params[0]["device_id"], params[1]["device_id"])
        self.assertNotEqual(params[1]["device_id"], params[2]["device_id"])
        self.assertEqual([item.key for item in saved], ["7300000000000000001", "7300000000000000002"])

    def test_a_refused_list_falls_back_to_scrolling_with_one_warning(self):
        for refusal in (FetchError("HTTP 403", status=403), {"statusCode": 10201, "itemList": []}):
            with self.subTest(refusal=refusal):
                saved, seen, tab = run("tik:someone", pages=DIRECT_HTML, feeds=FEEDS[:1], html=profile_page(),
                                       json={f"{API}/api/post/item_list/": refusal},
                                       options=Options(categories=("videos",)))
                self.assertEqual(len(saved), 6)
                self.assertEqual(seen.visited, [PROFILE])
                warnings = [m for m in seen.messages if m.startswith("!")]
                self.assertEqual(len(warnings), 1)
                self.assertIn("videos: the list API refused", warnings[0])

    def test_an_answer_that_isnt_json_falls_back_too(self):
        saved, seen, _ = run("tik:someone", pages={**DIRECT_HTML, f"{API}/api/post/item_list/": ""}, feeds=FEEDS[:1],
                             html=profile_page(), options=Options(categories=("videos",)))
        self.assertEqual(len(saved), 6)
        self.assertTrue(any("isn't JSON" in m for m in seen.messages))

    def test_an_empty_list_of_a_profile_with_videos_is_a_wall(self):
        """gallery-dl #9718: TikTok's first page can come back empty; the page is read instead."""
        saved, seen, _ = run("tik:someone", pages=DIRECT_HTML, feeds=FEEDS[:1], html=profile_page(),
                             json={f"{API}/api/post/item_list/": {"hasMore": False, "itemList": []}},
                             options=Options(categories=("videos",)))
        self.assertEqual(len(saved), 6)
        self.assertTrue(any("gave none of the 8 videos" in m for m in seen.messages))

    def test_a_private_tab_says_so_without_the_page(self):
        _, seen, _ = run("tik:someone", pages=DIRECT_HTML, options=Options(categories=("liked",)),
                         json={f"{API}/api/favorite/item_list/": {"hasMore": False, "itemList": []}})
        self.assertIn("liked: nothing public on this account", seen.messages)
        self.assertEqual(seen.visited, [])

    def test_a_refused_tab_is_read_by_its_click(self):
        saved, seen, _ = run("tik:someone", pages=DIRECT_HTML, feeds=[FEEDS[3]], html=TABS,
                             json={f"{API}/api/repost/item_list/": FetchError("HTTP 403", status=403)},
                             options=Options(categories=("reposts",)))
        self.assertEqual(len(saved), 4)
        self.assertEqual(seen.visited, [PROFILE])

    def test_html_without_a_sec_uid_reads_everything_in_the_browser(self):
        _, seen, _ = run("tik:someone", pages={PROFILE: profile_page()}, feeds=FEEDS, html=TABS,
                         options=Options(categories=("all",)))
        self.assertEqual([url for _, url, _ in seen.requests], [PROFILE])
        self.assertEqual(seen.visited, [PROFILE])


if __name__ == "__main__":
    unittest.main()
