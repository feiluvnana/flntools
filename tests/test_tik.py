"""TikTok: the profile's lists and tabs, the captcha, the picture, and one video from its embed page. No network."""

import json
import os
import time
import unittest
from datetime import datetime, timezone

from fakes import FakeSession

from flnd.session import Options
from flnd.source import Target, TargetError
from flnd.sources.tik import TIKTOK_UA, TikTok, best_video, posted_on

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


def profile_page(*tabs, video_count=2):
    data = {"__DEFAULT_SCOPE__": {"webapp.user-detail": {"userInfo": {
        "user": {"avatarLarger": "https://p16.example/tos-avt/abc123~tplv-cropcenter:1080:1080.jpeg?x=1"},
        "stats": {"videoCount": video_count}}}}}
    tab_html = "".join(f'<p data-e2e="{tab}">{tab}</p>' for tab in tabs)
    return (f'<html><body>{tab_html}<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
            f"{json.dumps(data)}</script></body></html>")


def clip(video_id, url):
    return {"id": video_id, "createTime": 1700000000, "video": {"playAddr": url}}


class TikTokTest(unittest.TestCase):
    def test_single_video_from_the_embed_page(self):
        video_id = "7234567890123456789"
        next_data = {"props": {"pageProps": {"itemStruct": {
            "id": video_id, "createTime": 1700000000, "video": {"playAddr": "https://v.example/clip.mp4"}}}}}
        page = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></html>'
        session = FakeSession(Target("tik", "video", video_id), cookies={"sessionid": "s", "tt_chain_token": "t"},
                              html={f"https://www.tiktok.com/embed/v2/{video_id}": page})
        TikTok().run(session)
        self.assertEqual(session.paths(), [f"videos/{DAY}_{video_id}.mp4"])
        self.assertEqual(session.saved[0]["headers"], {"Referer": "https://www.tiktok.com/", "User-Agent": TIKTOK_UA,
                                                       "Cookie": "tt_chain_token=t; sessionid=s"})
        self.assertEqual(session.requests[0][2], {"timeout": 20.0, "follow_redirects": False})

    def test_a_refused_embed_is_a_warning(self):
        session = FakeSession(Target("tik", "video", "7234567890123456789"))
        TikTok().run(session)
        self.assertEqual(session.saved, [])
        self.assertTrue(session.messages[0].startswith("! HTTP 404"))

    def test_profile_items_photos_stories_and_playlists(self):
        items = {"hasMore": False, "itemList": [
            {"id": "7300000000000000001", "createTime": 1700000000, "video": {"playAddr": "https://v/1.mp4"},
             "playlist": {"name": "Trip"}},
            {"id": "7300000000000000002", "createTime": 1700000000,
             "imagePost": {"images": [{"imageURL": {"urlList": ["https://i/a.jpg"]}},
                                      {"imageURL": {"urlList": ["https://i/b.jpg"]}}]}},
        ]}
        story = {"storyList": [{"id": "7300000000000000003", "createTime": 1700000000,
                                "video": {"playAddr": "https://v/s.mp4"}}]}

        def scrolled(session):
            session.respond("https://www.tiktok.com/api/post/item_list/?count=30", items)
            session.respond("https://www.tiktok.com/api/user/story/?id=1", story)

        session = FakeSession(Target("tik", "user", "someone"), on_scroll=scrolled)
        TikTok().run(session)
        stamp = DAY
        self.assertEqual(session.paths(), [f"albums/Trip/{stamp}_7300000000000000001.mp4",
                                           f"images/{stamp}_7300000000000000002_1.jpg",
                                           f"images/{stamp}_7300000000000000002_2.jpg",
                                           f"stories/{stamp}_7300000000000000003.mp4"])
        self.assertEqual([item["category"] for item in session.saved], ["playlists", "videos", "videos", "stories"])
        self.assertEqual(session.visited, ["https://www.tiktok.com/@someone"])

    def test_an_unsolved_captcha_backs_off_and_reloads_until_it_is_gone(self):
        class Challenged(FakeSession):
            def challenged(self):
                super().challenged()
                if self.challenges == 2:
                    self.page_html = "<body><p>feed</p></body>"

        session = Challenged(Target("tik", "user", "someone"),
                             page_html="<body><div id='captcha_container'>Drag the slider</div></body>")
        TikTok().run(session)
        self.assertEqual(session.challenges, 2)
        self.assertEqual(session.visited, ["https://www.tiktok.com/@someone"] * 3)

    def test_a_captcha_over_the_grid_is_waited_out_and_the_grid_read_again(self):
        class Challenged(FakeSession):
            def challenged(self):
                super().challenged()
                self.page_html = "<body><p>feed</p></body>"

        def scrolled(session):
            if session.challenges:
                session.respond("https://www.tiktok.com/api/post/item_list/?count=30",
                                {"hasMore": False, "itemList": [clip("7300000000000000001", "https://v/1.mp4")]})
            else:
                session.page_html = "<body><div id='captcha_container'>Drag the slider</div></body>"

        session = Challenged(Target("tik", "user", "someone"), on_scroll=scrolled, page_html="<body></body>")
        TikTok().run(session)
        self.assertEqual(session.challenges, 1)
        self.assertEqual(session.paths(), [f"videos/{DAY}_7300000000000000001.mp4"])

    def test_captcha_words_in_scripts_are_not_a_captcha(self):
        session = FakeSession(Target("tik", "user", "someone"),
                              page_html="<body><script>var t = 'Verify to continue';</script><p>feed</p></body>")
        TikTok().run(session)
        self.assertEqual(session.challenges, 0)

    def test_walled_api_falls_back_to_page_links(self):
        video_id = "7234567890123456789"
        next_data = {"props": {"pageProps": {"itemStruct": {
            "id": video_id, "createTime": 1700000000, "video": {"playAddr": "https://v.example/clip.mp4"}}}}}
        embed = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script>'
        session = FakeSession(Target("tik", "user", "someone"), html={f"https://www.tiktok.com/embed/v2/{video_id}": embed},
                              page_html=f'<body><a href="/@someone/video/{video_id}">clip</a></body>')
        TikTok().run(session)
        self.assertEqual(session.paths(), [f"videos/{DAY}_{video_id}.mp4"])

    def test_profile_picture_and_tabs(self):
        def scrolled(session):
            session.respond("https://www.tiktok.com/api/post/item_list/?count=30",
                            {"hasMore": False, "itemList": [clip("7300000000000000001", "https://v/1.mp4")]})
            session.respond("https://www.tiktok.com/api/repost/item_list/?count=30",
                            {"itemList": [clip("7300000000000000009", "https://v/9.mp4")]})

        session = FakeSession(Target("tik", "user", "someone"), options=Options(categories=("all",)), on_scroll=scrolled,
                              pages={PROFILE: profile_page("repost-tab", "liked-tab")})
        TikTok().run(session)
        self.assertEqual(session.paths(), ["profile/avatar_abc123.jpg", f"videos/{DAY}_7300000000000000001.mp4",
                                           f"reposts/{DAY}_7300000000000000009.mp4"])
        self.assertEqual([item["category"] for item in session.saved], ["profile", "videos", "reposts"])
        self.assertIn("videos: 1 found; the profile lists 2", session.messages)
        self.assertIn("favorites: not public on this account", session.messages)
        self.assertIn("liked: nothing public on this account", session.messages)
        self.assertIsNone(session.folder)

    def test_tabs_not_asked_for_are_not_read(self):
        session = FakeSession(Target("tik", "user", "someone"), options=Options(categories=("videos",)),
                              pages={PROFILE: profile_page("repost-tab")})
        TikTok().run(session)
        self.assertFalse(any("public" in message for message in session.messages))

    def test_the_best_video_address(self):
        self.assertEqual(best_video({"playAddr": {"urlList": ["https://v/a.mp4"]}}), "https://v/a.mp4")
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

    def test_dates_are_utc(self):
        """Fix #15: 2026-01-01 23:30 UTC is 2026-01-01 on every machine, not 2026-01-02 in Kiritimati (UTC+14)."""
        stamp = int(datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc).timestamp())
        with far_off_timezone("Pacific/Kiritimati"):
            self.assertEqual(posted_on({"createTime": stamp}, "1"), "2026-01-01")
            self.assertEqual(posted_on({}, str(stamp << 32)), "2026-01-01")
        stamp = int(datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc).timestamp())
        with far_off_timezone("America/Los_Angeles"):
            self.assertEqual(posted_on({"createTime": str(stamp)}, "1"), "2026-01-01")


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

if __name__ == "__main__":
    unittest.main()
