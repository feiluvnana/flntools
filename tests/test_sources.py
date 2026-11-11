"""The sources: the registry, their parsing, and each `run` through FakeSession. No network, no browser."""

import ast
import json
import unittest
from datetime import date, datetime
from pathlib import Path

from fakes import FakeSession

from flnd.session import Options
from flnd.sources import SOURCES
from flnd.sources.base import FetchError, Source
from flnd.sources.fbk import Facebook
from flnd.sources.ins import INSTAGRAM_MOBILE_UA, Instagram
from flnd.sources.tik import TIKTOK_UA, TikTok
from flnd.sources.twt import CHROME, Twitter
from flnd.target import Target

SOURCES_DIR = Path(__file__).resolve().parent.parent / "flnd" / "sources"
FORBIDDEN = ("httpx", "playwright", "shared.worker", "shared.kv", "shared.log", "shared.prompt",
             "shared._worker", "shared._kv", "shared._log", "shared._prompt")
FORBIDDEN_NAMES = {"worker", "kv", "log", "prompt"}


def day(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


class RegistryTest(unittest.TestCase):
    def test_every_source_describes_itself(self):
        for code, source in SOURCES.items():
            self.assertEqual(source.code, code)
            self.assertTrue(isinstance(source.name, str) and source.name)
            self.assertTrue(isinstance(source.domains, tuple) and source.domains, code)
            self.assertTrue(source.examples, code)
            self.assertTrue(set(source.short_links) <= {host for host in source.short_links
                                                        if any(host == d or host.endswith("." + d)
                                                               for d in source.domains)}, code)

    def test_codes_and_order(self):
        self.assertEqual(list(SOURCES), ["tik", "fbk", "ins", "twt"])

    def test_no_domain_belongs_to_two_sources(self):
        owners = {}
        for code, source in SOURCES.items():
            for domain in source.domains:
                self.assertNotIn(domain, owners, f"{domain} belongs to {owners.get(domain)} and {code}")
                owners[domain] = code

    def test_declarations(self):
        self.assertEqual(Instagram.needs, ("sessionid",))
        self.assertEqual(Twitter.needs, ("auth_token",))
        self.assertEqual((Twitter.user_agent, Twitter.stealth), (CHROME, False))
        self.assertEqual((TikTok.user_agent, TikTok.browser_args), (TIKTOK_UA, ("--no-sandbox",)))
        self.assertIsNone(Instagram.user_agent)
        for source in SOURCES.values():
            self.assertEqual((source.concurrency, source.pause, source.login_methods), (4, 5.0, ("browser", "file")))
            self.assertTrue(set(source.default_categories) <= set(source.categories), source.code)
        self.assertEqual(Instagram.categories, ("posts", "reels", "tagged", "reposts", "stories", "highlights"))
        self.assertEqual(Instagram.default_categories, ("posts", "reels", "stories", "highlights"))
        self.assertEqual(Facebook.categories, ("posts", "photos", "tagged", "albums", "reels", "videos"))
        self.assertNotIn("tagged", Facebook.default_categories)
        self.assertEqual((TikTok.categories, Twitter.categories), (("videos", "stories", "playlists"), ("posts", "media")))


class GuardTest(unittest.TestCase):
    """A source only talks to the session: it never imports the libraries the engine wraps."""

    def test_sources_import_nothing_the_engine_owns(self):
        for path in sorted(SOURCES_DIR.glob("*.py")):
            for imported in runtime_imports(ast.parse(path.read_text(encoding="utf-8"))):
                with self.subTest(module=path.name, imports=imported):
                    self.assertFalse(any(imported == name or imported.startswith(name + ".") for name in FORBIDDEN))


def runtime_imports(tree):
    """Every module a file imports when it runs; `if TYPE_CHECKING:` blocks are only read by checkers."""
    checking_only = {id(node) for block in ast.walk(tree) if isinstance(block, ast.If)
                     and ast.unparse(block.test) == "TYPE_CHECKING" for node in ast.walk(block)}
    for node in ast.walk(tree):
        if id(node) in checking_only:
            continue
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "shared":
                yield from (f"shared.{alias.name}" for alias in node.names if alias.name in FORBIDDEN_NAMES)
            yield node.module


class FacebookTest(unittest.TestCase):
    def test_tabs_are_scrolled_and_graphql_items_saved(self):
        photo = {"__typename": "Photo", "id": "11", "creation_time": 1700000000, "viewer_image": {"uri": "https://f/1.jpg"},
                 "album": {"name": "Summer: 2026"}}
        video = {"__typename": "Video", "id": "22", "publish_time": 1700000000,
                 "progressive_urls": [{"progressive_url": "https://f/sd.mp4", "metadata": {"quality": "SD"}},
                                      {"progressive_url": "https://f/hd.mp4", "metadata": {"quality": "HD"}}]}

        def scrolled(session):
            if session.folder == "photos":
                session.respond("https://www.facebook.com/api/graphql/", {"data": {"node": photo}})
            if session.folder == "videos":
                session.respond("https://www.facebook.com/api/graphql/", {"data": {"node": video}})
                session.respond("https://www.facebook.com/api/graphql/", ["not a dict"])

        session = FakeSession(Target("fbk", "user", "example.page"), on_scroll=scrolled)
        Facebook().run(session)
        self.assertEqual(session.visited[0], "https://www.facebook.com/example.page")
        self.assertEqual(session.visited[-1], "https://www.facebook.com/example.page/videos")
        self.assertEqual(session.paths(), [f"albums/Summer_2026/{day(1700000000)}_11.jpg",
                                           f"videos/{day(1700000000)}_22.mp4"])
        self.assertEqual([item["category"] for item in session.saved], ["photos", "videos"])
        self.assertEqual(session.saved[1]["source"], "https://f/hd.mp4")
        self.assertEqual(session.saved[0]["headers"], {"Referer": "https://www.facebook.com/example.page"})
        self.assertEqual(session.saved[0]["date"], date.fromisoformat(day(1700000000)))
        self.assertIsNone(session.folder)

    def test_tagged_is_opt_in(self):
        session = FakeSession(Target("fbk", "user", "a"))
        Facebook().run(session)
        self.assertNotIn("https://www.facebook.com/a/photos_of", session.visited)
        tagged = FakeSession(Target("fbk", "user", "a"), options=Options(categories=("tagged",)))
        Facebook().run(tagged)
        self.assertEqual(tagged.visited, ["https://www.facebook.com/a/photos_of"])

    def test_late_responses_are_ignored(self):
        session = FakeSession(Target("fbk", "user", "a"))
        Facebook().run(session)
        session.respond("https://www.facebook.com/api/graphql/",
                        {"data": {"__typename": "Photo", "id": "1", "image": {"uri": "https://f/x.jpg"}}})
        self.assertEqual(session.saved, [])


class InstagramTest(unittest.TestCase):
    def test_reposts_extraction(self):
        mock_data = {"data": {"fetch__XDTUserDict": {"user_reposts_timeline": {"repost_grid_items": [{"media": {
            "code": "TEST_REPOST_CODE", "pk": "123456789", "taken_at": 1700000000,
            "image_versions2": {"candidates": [{"url": "https://example.com/repost_img.jpg"}]},
        }}]}}}}
        extracted = Instagram()._extract_media_from_json(FakeSession(Target("ins", "user", "a")), mock_data,
                                                         tab="reposts")
        self.assertEqual(len(extracted), 1)
        self.assertEqual(extracted[0]["id"], "TEST_REPOST_CODE")
        self.assertIn("https://example.com/repost_img.jpg", extracted[0]["media_urls"])

    def test_stories_from_the_mobile_api(self):
        reels = {"reels_media": [{"items": [
            {"id": "301_42", "taken_at": 1700000000, "media_type": 2,
             "video_versions": [{"url": "https://cdn.example/s1.mp4"}]},
            {"pk": "302", "taken_at": 1700000000, "media_type": 1,
             "image_versions2": {"candidates": [{"url": "https://cdn.example/s2.jpg"}]}},
        ]}]}
        session = FakeSession(Target("ins", "user", "someone"), cookies={"sessionid": "s", "ds_user_id": "42"},
                              json={"https://i.instagram.com/api/v1/feed/reels_media/": reels})
        Instagram().run(session)
        self.assertEqual(session.paths()[:2], [f"stories/{day(1700000000)}_301.mp4", f"stories/{day(1700000000)}_302.jpg"])
        method, url, request = session.requests[0]
        self.assertEqual(request["params"]["user_ids"], '["42"]')
        self.assertEqual(request["headers"], {"user-agent": INSTAGRAM_MOBILE_UA, "cookie": "sessionid=s; ds_user_id=42",
                                              "x-ig-app-id": "936619743392459"})
        self.assertEqual(session.visited[0], "https://www.instagram.com/someone/")
        self.assertEqual(Instagram().headers({}), {"x-ig-app-id": "936619743392459"})

    def test_a_refused_story_tray_is_quiet(self):
        session = FakeSession(Target("ins", "user", "someone"), cookies={"ds_user_id": "42"},
                              json={"https://i.instagram.com/api/v1/feed/reels_media/":
                                    FetchError("HTTP 403", status=403)})
        Instagram().run(session)
        self.assertNotIn("! Could not fetch", " ".join(session.messages))

    def test_tabs_save_into_their_folder_and_category(self):
        timeline = {"data": {"user": {"edge_owner_to_timeline_media": {"edges": [
            {"node": {"shortcode": "ABC", "taken_at_timestamp": 1700000000, "display_url": "https://cdn/1.jpg"}},
            {"node": {"shortcode": "DEF", "taken_at_timestamp": 1700000000, "video_url": "https://cdn/2.mp4"}},
        ]}}}}

        def scrolled(session):
            if session.folder == "posts":
                session.respond("https://www.instagram.com/graphql/query", timeline)

        session = FakeSession(Target("ins", "user", "someone"), on_scroll=scrolled)
        Instagram().run(session)
        self.assertEqual(session.paths(), [f"posts/{day(1700000000)}_ABC.jpg", f"posts/{day(1700000000)}_DEF.mp4"])
        self.assertEqual({item["category"] for item in session.saved}, {"posts"})
        self.assertEqual(session.saved[0]["headers"], {"Referer": "https://www.instagram.com/someone/"})
        # Other people's content, tagged and reposts, is opt-in.
        self.assertEqual(session.visited, ["https://www.instagram.com/someone/", "https://www.instagram.com/someone/reels/"])

    def test_from_picks_the_tabs(self):
        session = FakeSession(Target("ins", "user", "someone"), options=Options(categories=("reels", "tagged")))
        Instagram().run(session)
        self.assertEqual(session.visited, ["https://www.instagram.com/someone/reels/",
                                           "https://www.instagram.com/someone/tagged/"])

    def test_highlights_alone_still_load_the_profile(self):
        session = FakeSession(Target("ins", "user", "someone"), cookies={"ds_user_id": "42"},
                              options=Options(categories=("highlights",)))
        Instagram().run(session)
        self.assertEqual(session.visited, ["https://www.instagram.com/someone/"])
        self.assertEqual(session.requests, [])

    def test_stories_alone_never_open_the_browser(self):
        session = FakeSession(Target("ins", "user", "someone"), cookies={"ds_user_id": "42"},
                              json={"https://i.instagram.com/api/v1/feed/reels_media/": {"reels_media": []}},
                              options=Options(categories=("stories",)))
        Instagram().run(session)
        self.assertEqual(session.visited, [])
        self.assertEqual(len(session.requests), 1)


class TwitterTest(unittest.TestCase):
    def test_timeline_parsing(self):
        data = timeline([
            {"entryId": "tweet-111", "content": {"itemContent": tweet("111222333", [
                {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/test.jpg"}])}},
            {"entryId": "cursor-bottom-123", "content": {"cursorType": "Bottom", "value": "CURSOR_XYZ"}},
        ])
        items, cursor = Twitter()._parse_timeline_entries(FakeSession(Target("twt", "user", "a")), data)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tweet_id"], "111222333")
        self.assertEqual(items[0]["kind"], "image")
        self.assertEqual(cursor, "CURSOR_XYZ")

    def test_user_timelines_from_the_graphql_api(self):
        photo = {"type": "photo", "media_url_https": "https://pbs.example/p.jpg"}
        video = {"type": "video", "video_info": {"variants": [
            {"content_type": "video/mp4", "bitrate": 1, "url": "https://pbs.example/low.mp4"},
            {"content_type": "video/mp4", "bitrate": 9, "url": "https://pbs.example/high.mp4"},
        ]}}
        api = "https://x.com/i/api/graphql/"
        session = FakeSession(Target("twt", "user", "someone"), json={
            api + "AMIBMjtxEEATh4z8V9GtRg/UserByScreenName": {"data": {"user": {"result": {"rest_id": "42"}}}},
            api + "ZFKu1DFS63hgrkLzR3Hsdw/UserOriginalsTimeline": timeline([
                {"entryId": "tweet-111", "content": {"itemContent": tweet("111", [photo])}},
                {"entryId": "cursor-bottom-0", "content": {"cursorType": "Bottom", "value": "0|end"}},
            ]),
            # The media tab lists the same photo again, which must not be saved twice.
            api + "jCRhbOzdgOHp6u9H4g2tEg/UserMedia": timeline([{"entryId": "grid", "content": {"items": [
                {"item": {"itemContent": tweet("111", [photo])}},
                {"item": {"itemContent": tweet("222", [video])}},
            ]}}]),
        })
        Twitter().run(session)
        self.assertEqual(session.paths(), ["posts/2026-11-09_111_0.jpg", "videos/2026-11-09_222_0.mp4"])
        self.assertEqual([item["source"] for item in session.saved],
                         ["https://pbs.example/p.jpg?name=orig", "https://pbs.example/high.mp4"])
        self.assertEqual([item["key"] for item in session.saved], ["111_0", "222_0"])
        variables = json.loads(session.requests[1][2]["params"]["variables"])
        self.assertEqual(variables["userId"], "42")
        self.assertTrue(variables["withQuickPromoteEligibilityTweetFields"])
        self.assertEqual(session.visited, [])

    def test_from_media_reads_only_the_media_timeline(self):
        session = FakeSession(Target("twt", "user", "42"), options=Options(categories=("media",)), json={
            "https://x.com/i/api/graphql/jCRhbOzdgOHp6u9H4g2tEg/UserMedia": timeline([]),
        })
        Twitter().run(session)
        self.assertEqual([url.rsplit("/", 1)[-1] for _, url, _ in session.requests], ["UserMedia"])

    def test_a_user_id_skips_the_lookup(self):
        session = FakeSession(Target("twt", "user", "42"), json={
            "https://x.com/i/api/graphql/ZFKu1DFS63hgrkLzR3Hsdw/UserOriginalsTimeline": timeline([]),
            "https://x.com/i/api/graphql/jCRhbOzdgOHp6u9H4g2tEg/UserMedia": timeline([]),
        })
        Twitter().run(session)
        self.assertNotIn("UserByScreenName", " ".join(url for _, url, _ in session.requests))
        self.assertEqual(json.loads(session.requests[0][2]["params"]["variables"])["userId"], "42")

    def test_a_refused_api_switches_to_the_browser(self):
        refused = FetchError("HTTP 403", status=403)
        api = "https://x.com/i/api/graphql/"
        session = FakeSession(Target("twt", "user", "someone"), json={
            api + "AMIBMjtxEEATh4z8V9GtRg/UserByScreenName": {"data": {"user": {"result": {"rest_id": "42"}}}},
            api + "ZFKu1DFS63hgrkLzR3Hsdw/UserOriginalsTimeline": refused,
        }, on_scroll=lambda session: session.respond(
            "https://x.com/i/api/graphql/x/UserMedia?variables=",
            timeline([{"entryId": "t", "content": {"itemContent": tweet("333", [
                {"type": "photo", "media_url_https": "https://pbs.example/b.jpg"}])}}])))
        Twitter().run(session)
        self.assertEqual(session.visited, ["https://x.com/someone", "https://x.com/someone/media"])
        self.assertEqual(session.paths(), ["posts/2026-11-09_333_0.jpg"])

    def test_headers_carry_the_session(self):
        headers = Twitter().headers({"auth_token": "a", "ct0": "c"})
        self.assertEqual((headers["x-csrf-token"], headers["x-twitter-auth-type"], headers["cookie"]),
                         ("c", "OAuth2Session", "auth_token=a; ct0=c"))
        self.assertEqual(Twitter().headers({})["x-twitter-auth-type"], "")


class TikTokTest(unittest.TestCase):
    def test_single_video_from_the_embed_page(self):
        video_id = "7234567890123456789"
        next_data = {"props": {"pageProps": {"itemStruct": {
            "id": video_id, "createTime": 1700000000, "video": {"playAddr": "https://v.example/clip.mp4"}}}}}
        page = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></html>'
        session = FakeSession(Target("tik", "video", video_id), cookies={"sessionid": "s", "tt_chain_token": "t"},
                              html={f"https://www.tiktok.com/embed/v2/{video_id}": page})
        TikTok().run(session)
        self.assertEqual(session.paths(), [f"videos/{day(1700000000)}_{video_id}.mp4"])
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
        stamp = day(1700000000)
        self.assertEqual(session.paths(), [f"albums/Trip/{stamp}_7300000000000000001.mp4",
                                           f"images/{stamp}_7300000000000000002_1.jpg",
                                           f"images/{stamp}_7300000000000000002_2.jpg",
                                           f"stories/{stamp}_7300000000000000003.mp4"])
        self.assertEqual([item["category"] for item in session.saved], ["playlists", "videos", "videos", "stories"])
        self.assertEqual(session.visited, ["https://www.tiktok.com/@someone"])

    def test_unsolved_captcha_is_reported_as_a_challenge(self):
        session = FakeSession(Target("tik", "user", "someone"),
                              page_html="<body><div id='captcha_container'>Drag the slider</div></body>")
        TikTok().run(session)
        self.assertEqual(session.challenges, 1)

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
        self.assertEqual(session.paths(), [f"videos/{day(1700000000)}_{video_id}.mp4"])


class SourceDefaultsTest(unittest.TestCase):
    def test_logged_in_needs_any_one_cookie(self):
        self.assertFalse(TikTok().logged_in(FakeSession(Target("tik", "account", ""))))
        self.assertTrue(TikTok().logged_in(FakeSession(Target("tik", "account", ""), cookies={"sid_tt": "1"})))

    def test_a_source_without_needs_is_always_logged_in(self):
        class Open(Source):
            code, name, domains = "opn", "Open", ("open.example",)

            def resolve(self, text):
                return Target(self.code, "user", text)

            def run(self, session):
                pass

        account = FakeSession(Target("opn", "account", ""), source=Open)
        self.assertTrue(Open().logged_in(account))
        request = object()
        self.assertIs(Open().sign(request), request)
        with Open().connect(account) as client:
            self.assertIsNone(client)


def tweet(tweet_id, media):
    return {"tweet_results": {"result": {"rest_id": tweet_id, "legacy": {
        "created_at": "Mon Nov 09 10:00:00 +0000 2026", "extended_entities": {"media": media}}}}}


def timeline(entries):
    return {"data": {"user": {"result": {"timeline_v2": {"timeline": {"instructions": [{"entries": entries}]}}}}}}


if __name__ == "__main__":
    unittest.main()
