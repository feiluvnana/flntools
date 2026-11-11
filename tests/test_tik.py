"""TikTok: the profile's lists and tabs, the captcha, the picture, and one video from its embed page. No network."""

import json
import unittest
from datetime import datetime

from fakes import FakeSession

from flnd.session import Options
from flnd.sources.tik import TIKTOK_UA, TikTok, best_video
from flnd.target import Target

DAY = datetime.fromtimestamp(1700000000).strftime("%Y-%m-%d")
PROFILE = "https://www.tiktok.com/@someone"


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

    def test_tabs_are_opt_in(self):
        session = FakeSession(Target("tik", "user", "someone"), pages={PROFILE: profile_page("repost-tab")})
        TikTok().run(session)
        self.assertFalse(any("public" in message for message in session.messages))

    def test_the_best_video_address(self):
        self.assertEqual(best_video({"playAddr": {"urlList": ["https://v/a.mp4"]}}), "https://v/a.mp4")
        self.assertEqual(best_video({"bitrateInfo": [{"Bitrate": 1, "PlayAddr": {"UrlList": ["https://v/low.mp4"]}},
                                                     {"Bitrate": 9, "PlayAddr": {"UrlList": ["https://v/high.mp4"]}}]}),
                         "https://v/high.mp4")
        self.assertIsNone(best_video({}))


if __name__ == "__main__":
    unittest.main()
