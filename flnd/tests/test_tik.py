"""TikTok on plugin API 2 (src/plugins/tik.py): its routes, names and keys, the profile's page and
feeds, the captcha, the tabs, and one post from its embed page.

Everything runs on `FakeCtx`: the site's answers are `Reply`s, the profile's tab a `FakePage`
whose feeds hold the JSON its own scripts would fetch. No network, browser or wait.
"""

import asyncio
import datetime
import json
import os
import re
import time
import unittest
from unittest import mock

import no_profiles  # noqa: F401
from fakes import filed
from plugin_kit import FakeCtx, FakePage, Reply, Sent, check, collect

from flnd.engine.plugins import PLUGINS
from flnd.engine.resolve import Manifest, resolve
from flnd.errors import (
    Blocked,
    Changed,
    Hidden,
    NetworkError,
    NotFound,
    PluginError,
    SiteError,
    UsageError,
)
from flnd.plugin import Range
from flnd.plugins.tik import Profile, TikTok, best_video
from flnkit import html

API = "https://www.tiktok.com"
PROFILE = f"{API}/@example_creator"
CDN = "https://cdn.example.com"
VIDEO = "7234567890123456789"
CAPTCHA = "<body><div id='captcha_container'>Drag the slider</div></body>"


def video(video_id, url, **extra):
    return {"id": video_id, "video": {"playAddr": url}, **extra}


def photo(video_id, *urls, **extra):
    images = [{"imageURL": {"urlList": [url]}} for url in urls]
    return {"id": video_id, "imagePost": {"images": images}, **extra}


def embed_page(item):
    data = json.dumps({"props": {"pageProps": {"itemStruct": item}}})
    return f'<html><script id="__NEXT_DATA__" type="application/json">{data}</script></html>'


def profile_page(*tabs, video_count=2, body=""):
    user = {"avatarLarger": "https://p16.example.com/tos-avt/abc123~tplv-c:1080:1080.jpeg?x=1"}
    data = json.dumps({"__DEFAULT_SCOPE__": {"webapp.user-detail": {"userInfo": {
        "user": user, "stats": {"videoCount": video_count}}}}})
    tab_html = "".join(f'<p data-e2e="{tab}">{tab}</p>' for tab in tabs)
    return (f"<html><body>{tab_html}{body}<script id=\"__UNIVERSAL_DATA_FOR_REHYDRATION__\" "
            f'type="application/json">{data}</script></body></html>')


# What the profile answers, newest first: dates read from the id (createTime 0 or none), a
# playlist with a `/`, carousels, and a video repeated in the reposts.
POSTS = {"hasMore": False, "itemList": [
    video("7300000000000000001", f"{CDN}/v1", createTime=1700000000),
    video("7300000000000000002", f"{CDN}/v2", createTime=1700000001,
          playlist={"name": "Road Trip"}),
    photo("7300000000000000003", f"{CDN}/p1a.jpeg", f"{CDN}/p1b", createTime="1700000500"),
    photo("7300000000000000004", f"{CDN}/p2a", f"{CDN}/p2b.jpg", createTime=1700000600,
          playlist={"name": "A/B"}),
    {"id": "7300000000000000006", "createTime": 1700000700, "video": {"bitrateInfo": [
        {"Bitrate": 9, "PlayAddr": {"Width": 1, "Height": 1, "UrlList": [f"{CDN}/best"]}}]}},
    video("7300000000000000005", f"{CDN}/v3", createTime=0),
    video("5", f"{CDN}/v4"),
    {"id": "7300000000000000007", "createTime": 1700000800},
]}
REPOSTS = '[data-e2e="repost-tab"]'
# The post list and the stories come with the load; each tab's list on its click.
FEEDS = [
    (f"{API}/api/post/item_list/?count=30", POSTS),
    (f"{API}/api/story/item_list/?id=1", {"storyList": [
        video("7310000000000000001", f"{CDN}/s1", createTime=1700001000),
        photo("7310000000000000002", f"{CDN}/s2.jpeg", createTime=1700001001)]}),
    (f"{API}/api/user/story/?id=1", {"story_list": [
        {"aweme_id": "7310000000000000003", "create_time": 1700001002,
         "video": {"playAddr": f"{CDN}/s3"}}]}),
    Sent(f"{API}/api/repost/item_list/?count=30", click=REPOSTS, body={"itemList": [
        video("7300000000000000001", f"{CDN}/v1", createTime=1700000000),
        video("7320000000000000001", f"{CDN}/r1", createTime=1700002000,
              playlist={"name": "Ignored"}),
        photo("7320000000000000002", f"{CDN}/r2a.jpeg", f"{CDN}/r2b",
              createTime=1700002001)]}),
    Sent(f"{API}/api/user/collect/item_list/?count=30", click='[data-e2e="favorites-tab"]',
         body={"itemList": [video("7330000000000000001", f"{CDN}/f1", createTime=1700003000)]}),
    Sent(f"{API}/api/favorite/item_list/?count=30", click='[data-e2e="liked-tab"]',
         body={"itemList": [photo("7340000000000000001", f"{CDN}/l1a",
                                  createTime=1700004000)]}),
]
TABS = profile_page("repost-tab", "favorites-tab", "liked-tab", video_count=8)

# The (path, key) pairs the API-1 source saved for the same answers (tests/test_tik.py). A
# photo's and the avatar's names are pinned; a video's extension is the content's, so its path
# has none here.
PROFILE_PAIRS = [
    ("profile/avatar_abc123.jpg", "profile/avatar_abc123.jpg"),
    ("videos/2023-11-14_7300000000000000001", "7300000000000000001"),
    ("albums/Road_Trip/2023-11-14_7300000000000000002", "7300000000000000002"),
    ("images/2023-11-14_7300000000000000003_1.jpg", "7300000000000000003_1"),
    ("images/2023-11-14_7300000000000000003_2.jpg", "7300000000000000003_2"),
    ("albums/A_B/2023-11-14_7300000000000000004_1.jpg", "7300000000000000004_1"),
    ("albums/A_B/2023-11-14_7300000000000000004_2.jpg", "7300000000000000004_2"),
    ("videos/2023-11-11_7300000000000000005", "7300000000000000005"),
    ("videos/5", "5"),
    ("videos/2023-11-14_7300000000000000006", "7300000000000000006"),
    ("stories/2023-11-14_7310000000000000001", "7310000000000000001"),
    ("stories/2023-11-14_7310000000000000002_1.jpg", "7310000000000000002_1"),
    ("stories/2023-11-14_7310000000000000003", "7310000000000000003"),
    ("reposts/2023-11-14_7320000000000000001", "7320000000000000001"),
    ("reposts/2023-11-14_7320000000000000002_1.jpg", "7320000000000000002_1"),
    ("reposts/2023-11-14_7320000000000000002_2.jpg", "7320000000000000002_2"),
    ("favorites/2023-11-14_7330000000000000001", "7330000000000000001"),
    ("liked/2023-11-14_7340000000000000001_1.jpg", "7340000000000000001_1"),
]
EMBEDS = {
    "7234567890123456789": (video(VIDEO, f"{CDN}/e1", createTime=1700000000),
                            [("7234567890123456789/videos/2023-11-14_7234567890123456789", VIDEO)]),
    "7234567890123456790": (photo("7234567890123456790", f"{CDN}/e2a.jpeg", f"{CDN}/e2b"),
                            [("7234567890123456790/images/2023-05-18_7234567890123456790_1.jpg",
                              "7234567890123456790_1"),
                             ("7234567890123456790/images/2023-05-18_7234567890123456790_2.jpg",
                              "7234567890123456790_2")]),
    "7234567890123456791": (video("7234567890123456791", f"{CDN}/e3.mp4",
                                  createTime="1700000100", playlist={"name": "Trip/2: A"}),
                            [("7234567890123456791/albums/Trip_2_A/"
                              "2023-11-14_7234567890123456791.mp4",
                              "7234567890123456791")]),
}


def fake(html_page=TABS, feeds=(), *, ctx=FakeCtx, shown=None, **given):
    """A `FakeCtx` (or subclass) for the profile, its page showing `html_page` and `feeds`.

    Every tab in the HTML is shown, unless `shown` lists the selectors that are."""
    if shown is None:
        shown = [f'[data-e2e="{name}"]' for name in re.findall(r'data-e2e="([^"]+)"', html_page)]
    return ctx(TikTok, "tik:example_creator",
               pages={PROFILE: FakePage(html_page, feeds=feeds, shown=shown)}, **given)


def embedded(item, **given):
    return FakeCtx(TikTok, routes={f"{API}/embed/v2/{item['id']}": Reply(embed_page(item))},
                   **given)


def pairs(saved):
    return [(found.path, found.key) for found in saved]


def listed(ctx, category):
    """One category's posts on `ctx`, and the error that ended it (None when none did)."""
    async def run():
        plugin, found = TikTok(), []
        profile = await plugin.user(ctx, "example_creator")
        try:
            async for post in getattr(plugin, category)(ctx, profile):
                found.append(post)
        except Exception as error:  # noqa: BLE001 (returned for the test to check)
            return found, error
        return found, None

    return asyncio.run(run())


class Watching(FakeCtx):
    """Records whether each feed opened would take the post list's answers."""

    def feed(self, match, **options):
        self.watched = [*getattr(self, "watched", []), match(FEEDS[0][0])]
        return super().feed(match, **options)


def listed_targets(stream):
    """The targets a discovering route yields, and the error that ended it (None when none)."""
    async def run():
        found = []
        try:
            async for target in stream:
                found.append(target)
        except Exception as error:  # noqa: BLE001 (returned for the test to check)
            return found, error
        return found, None

    return asyncio.run(run())


class RoutesTest(unittest.TestCase):
    def test_every_form(self):
        # (text, (kind, value)), or None for a form no route reads; the kinds and values are
        # the keys of the stored targets and the history, so a change here is a migration.
        for text, expected in [
            ("tik:example_creator", ("user", "example_creator")),
            ("tik:@example_creator", ("user", "example_creator")),
            ("example_creator", ("user", "example_creator")),
            ("tik:example_user", ("user", "example_user")),
            ("tik:123456789012345", ("user", "123456789012345")),
            ("https://www.tiktok.com/@example_creator", ("user", "example_creator")),
            ("https://www.tiktok.com/@example_creator?lang=en", ("user", "example_creator")),
            ("https://m.tiktok.com/@example_creator/", ("user", "example_creator")),
            ("https://www.tiktok.com/@example_creator/liked", ("user", "example_creator")),
            ("https://www.tiktok.com/@example_creator/video/abc", ("user", "example_creator")),
            ("https://www.tiktok.com/@example_creator/x/7301234567890123456",
             ("user", "example_creator")),
            ("tik:post/7301234567890123456", ("post", "post/7301234567890123456")),
            ("tik:7301234567890123456", ("user", "7301234567890123456")),
            (f"https://www.tiktok.com/@example_creator/photo/{VIDEO}", ("post", f"post/{VIDEO}")),
            (f"https://www.tiktok.com/@example_creator/video/{VIDEO}/?lang=en",
             ("post", f"post/{VIDEO}")),
            (f"https://www.tiktok.com/@example_creator/video/{VIDEO}/photo/1",
             ("post", f"post/{VIDEO}")),
            ("https://www.tiktok.com/@example_creator/video/0", ("post", "post/0")),
            (f"https://www.tiktok.com/video/{VIDEO}", ("post", f"post/{VIDEO}")),
            (f"https://www.tiktok.com/photo/{VIDEO}", ("post", f"post/{VIDEO}")),
            ("tik:not a name", None), ("tik:ex-ample", None), ("tik:@@example_creator", None),
            ("tik:", None), ("tik:https://www.tiktok.com/", None),
            ("https://www.tiktok.com/example_creator", None),
            ("https://www.tiktok.com/@my-user", None), ("https://www.tiktok.com/@", None),
            ("https://www.tiktok.com/@@example_creator", None),
            ("https://www.tiktok.com/video/", None),
            ("https://vm.tiktok.com/ZMabc123/", ("short", "short/ZMabc123")),
            ("vt.tiktok.com/ZSabc-1", ("short", "short/ZSabc-1")),
            ("https://www.tiktok.com/t/ZTabc123/", ("short", "short/ZTabc123")),
            ("tik:me", ("user", "me")),
            ("notiktok.com/@example_creator", None), ("tiktok.com.evil.example/@x", None),
        ]:
            with self.subTest(text=text):
                found = TikTok.resolve(text)
                self.assertEqual(found and (found.kind, found.value), expected)

    def test_refused_text(self):
        for text, message in [
            ("https://www.tiktok.com/tag/cats", "hashtag pages"),
            ("https://www.tiktok.com/music/Song-123", "sound pages"),
            ("https://www.tiktok.com/discover/cats", "discover pages"),
            ("https://www.tiktok.com/live", "live streams"),
            ("https://www.tiktok.com/explore", "the explore page"),
            ("https://www.tiktok.com/search?q=x", "search pages"),
            ("https://www.tiktok.com/search/video?q=x", "search pages"),
        ]:
            with self.subTest(text=text), self.assertRaisesRegex(UsageError, message):
                TikTok.resolve(text)

    def test_a_search_is_refused_before_any_request(self):
        manifest = Manifest(PLUGINS)
        for text in ("tik?x", "https://www.tiktok.com/search?q=x"):
            with self.subTest(text=text), self.assertRaises(UsageError):
                resolve(text, manifest=manifest, known={})

    def test_the_declarations(self):
        self.assertEqual([str(found) for found in check(TikTok)][:2], [
            "tik:post/7301234567890123456", "tik:example_creator"])
        user = next(known for known in TikTok.routes if known.kind == "user")
        self.assertEqual([(c.name, c.own, c.newest_first) for c in user.categories], [
            ("videos", True, True), ("stories", True, False), ("playlists", True, True),
            ("reposts", False, False), ("liked", False, False), ("favorites", False, False),
            ("profile", True, False)])
        self.assertTrue(TikTok.routes[0].single)


class NamesAndKeysTest(unittest.TestCase):
    """No history migration: every key is what the API-1 source gave for the same answers."""

    def test_a_single_post_from_its_embed_page(self):
        for item, expected in EMBEDS.values():
            with self.subTest(post=item["id"]):
                ctx = embedded(item)
                saved = collect(TikTok, f"tik:post/{item['id']}", ctx=ctx)
                self.assertEqual(pairs(saved), expected)

    def test_a_caption_becomes_the_title_and_never_changes_a_key(self):
        caption = "Sunset at the beach #fun\nsecond line"
        for item, path, key in (
            (video(VIDEO, f"{CDN}/v", desc=caption), "videos/Sunset_at_the_beach_", VIDEO),
            (photo(VIDEO, f"{CDN}/p", desc=caption), "images/Sunset_at_the_beach_", f"{VIDEO}_1"),
            (video(VIDEO, f"{CDN}/v", desc="#fun @friend"), "videos/", VIDEO),
            (photo(VIDEO, f"{CDN}/p", desc=""), "images/", f"{VIDEO}_1"),
            (video(VIDEO, f"{CDN}/v", desc="x" * 60), f"videos/{'x' * 40}_", VIDEO),
        ):
            with self.subTest(path=path):
                saved = collect(TikTok, f"tik:post/{VIDEO}", ctx=embedded(item))
                [(found, found_key)] = pairs(saved)
                kind, _, rest = path.partition("/")
                name = f"{VIDEO}/{kind}/2023-05-18_{rest}{VIDEO}" + ("_1.jpg" if "_1" in key else "")
                self.assertEqual((found, found_key), (name, key))

    def test_a_post_link_is_filed_by_its_creator_and_kind(self):
        link, creator = f"{API}/@example_creator/video/{VIDEO}", {"author": {"uniqueId": "me"}}
        rows = [(video(VIDEO, f"{CDN}/v", **creator), "me/videos"),
                (photo(VIDEO, f"{CDN}/p", **creator), "me/images"),
                (video(VIDEO, f"{CDN}/v"), f"{VIDEO}/videos")]
        for item, folder in rows:
            with self.subTest(folder=folder):
                [found] = collect(TikTok, link, ctx=embedded(item))
                self.assertEqual(filed(TikTok, link, found), folder)

    def test_a_profile_with_every_category_once_opened(self):
        ctx = fake(feeds=FEEDS)
        saved = collect(TikTok, "tik:example_creator", ctx=ctx, categories=["all"])
        self.assertEqual(sorted(pairs(saved)), sorted(PROFILE_PAIRS))
        self.assertEqual(ctx.seen.visited, [PROFILE])

    def test_the_dates_are_utc_days(self):
        late = int(datetime.datetime(2026, 1, 1, 23, 30, tzinfo=datetime.UTC).timestamp())
        feeds = [(FEEDS[0][0], {"hasMore": False, "itemList": [
            video("1", f"{CDN}/a", createTime=late), video(str(late << 32), f"{CDN}/c")]})]
        if not hasattr(time, "tzset"):
            self.skipTest("no time.tzset on this platform")
        with mock.patch.dict(os.environ, {"TZ": "Pacific/Kiritimati"}):
            time.tzset()
            saved = collect(TikTok, "tik:example_creator", ctx=fake(profile_page(), feeds),
                            categories=["videos"])
        time.tzset()
        self.assertEqual(pairs(saved), [("videos/2026-01-01_1", "1"),
                                        (f"videos/2026-01-01_{late << 32}", str(late << 32))])


class EmbedTest(unittest.TestCase):
    def test_the_cdn_is_sent_only_the_cookies_that_have_a_value(self):
        item = EMBEDS[VIDEO][0]
        for cookies, sent in (({"sid_tt": "t"}, None),
                              ({"sessionid": "", "tt_chain_token": "c"}, "tt_chain_token=c"),
                              ({"sessionid": "s", "tt_chain_token": "t"},
                               "tt_chain_token=t; sessionid=s")):
            with self.subTest(cookies=cookies):
                saved = collect(TikTok, f"tik:post/{VIDEO}", ctx=embedded(item, cookies=cookies))
                headers = dict(saved[0].post.headers)
                self.assertEqual(headers.pop("Cookie", None), sent)
                self.assertEqual(headers, {"Referer": f"{API}/"})
        self.assertTrue(asyncio.run(TikTok.login.check(FakeCtx(TikTok, cookies={"sid_tt": "t"}))))

    def test_a_refused_embed_page_fails_the_post(self):
        """S1: a 404 is `NotFound`, recorded gone, not a warning counted done; the public page's
        401 or 403 is no login wall."""
        for reply, error in ((Reply(status=404), NotFound), (Reply(status=410), NotFound),
                             (Reply(status=403), SiteError), (Reply("<html></html>"), Changed)):
            with self.subTest(status=reply.status):
                ctx = FakeCtx(TikTok, routes={f"{API}/embed/v2/{VIDEO}": reply})
                with self.assertRaises(error) as raised:
                    collect(TikTok, f"tik:post/{VIDEO}", ctx=ctx)
                self.assertIs(type(raised.exception), error)

    def test_refresh_is_one_embed_fetch_for_the_file(self):
        item = photo(VIDEO, f"{CDN}/new1", f"{CDN}/new2", createTime=1700000000)
        ctx = embedded(item)
        stale = collect(TikTok, f"tik:post/{VIDEO}", ctx=embedded(item))[1].post
        fresh = asyncio.run(TikTok().refresh(ctx, stale))
        self.assertEqual([file.url for file in fresh.files], [f"{CDN}/new2"])
        self.assertEqual(len(ctx.seen.requests), 1)
        avatar = collect(TikTok, "tik:example_creator", ctx=fake(), categories=["profile"])
        with self.assertRaises(NotFound):
            asyncio.run(TikTok().refresh(ctx, avatar[0].post))


class ProfileTest(unittest.TestCase):
    def test_a_playlist_item_is_in_playlists_and_not_in_videos(self):
        for category, count in (("videos", 6), ("playlists", 3)):
            with self.subTest(category=category):
                saved = collect(TikTok, "tik:example_creator", ctx=fake(feeds=FEEDS[:1]),
                                categories=[category])
                self.assertEqual({found.path.startswith("albums/") for found in saved},
                                 {category == "playlists"})
                self.assertEqual(len(saved), count)

    def test_unwanted_categories_are_not_queued_in_profile(self):
        ctx = fake(feeds=FEEDS[:1])
        profile = Profile("example_creator", wanted={"videos"})

        async def run_videos():
            return [p async for p in profile.posts(ctx, "videos")]

        asyncio.run(run_videos())
        self.assertEqual(len(profile._queues["playlists"]), 0)

    def test_an_older_pinned_post_above_newer_ones(self):
        # Marked pinned, it sits outside the date order; unmarked, the order is broken.
        for pinned, error in ((True, None), (False, PluginError)):
            with self.subTest(pinned=pinned):
                feeds = [(FEEDS[0][0], {"hasMore": False, "itemList": [
                    video("7200000000000000001", f"{CDN}/old", createTime=1690000000,
                          isPinnedItem=pinned),
                    video("7300000000000000001", f"{CDN}/v1", createTime=1700000001),
                    video("7300000000000000002", f"{CDN}/v2", createTime=1700000000)]})]
                ctx = fake(profile_page(), feeds)
                try:
                    saved, raised = collect(TikTok, "tik:example_creator", ctx=ctx,
                                            categories=["videos"]), None
                except PluginError as found:
                    saved, raised = [], found
                self.assertIs(type(raised) if raised else None, error)
                self.assertEqual([found.post.pinned for found in saved],
                                 [True, False, False] if pinned else [])

    def test_stories_alone_open_no_post_list_feed(self):
        """S10: `stories` loads with its own feed, and nothing scrolls."""
        ctx = fake(feeds=FEEDS, ctx=Watching)
        saved = collect(TikTok, "tik:example_creator", ctx=ctx, categories=["stories"])
        self.assertEqual(len(saved), 3)
        self.assertEqual((ctx.watched, ctx.seen.scrolls, ctx.seen.visited),
                         ([False], 0, [PROFILE]))

    def test_the_engine_stops_the_scroll_after_five_posts_older_than_when(self):
        old = [video(f"730000000000000001{n}", f"{CDN}/o{n}", createTime=1699000000)
               for n in range(5)]
        feeds = [(f"{API}/api/post/item_list/?p=1", {"hasMore": True, "itemList": [
                      video("7300000000000000001", f"{CDN}/new", createTime=1700000000)]}),
                 Sent(f"{API}/api/post/item_list/?p=2", {"hasMore": True, "itemList": old},
                      scroll=1),
                 Sent(f"{API}/api/post/item_list/?p=3", {"hasMore": False, "itemList": [
                      video("7300000000000000099", f"{CDN}/later", createTime=1700000000)]},
                      scroll=2)]
        when = Range(since=datetime.date(2023, 11, 14))
        ctx = fake(profile_page(), feeds, when=when)
        saved = collect(TikTok, "tik:example_creator", ctx=ctx, categories=["videos"])
        self.assertEqual([found.key for found in saved], ["7300000000000000001"])
        self.assertIn(("info", "videos: 6 found; the profile lists 2"), ctx.seen.logs)
        self.assertEqual(ctx.seen.scrolls, 1)

    def test_a_walled_post_list(self):
        """S19: the posts the page links to are saved, then the list is `Hidden`, in `videos`
        and in `playlists`, which leaves the links to `videos`. A list answered empty is walled
        too; a profile listing no videos is just empty."""
        link = f'<a href="/@example_creator/video/{VIDEO}">clip</a>'
        routes = {f"{API}/embed/v2/{VIDEO}": Reply(embed_page(EMBEDS[VIDEO][0]))}
        empty = [(FEEDS[0][0], {"hasMore": False, "itemList": []})]
        other = "log in with another account, or try again later"
        playlists = ("log in with another account, or download its videos category too: it "
                     "reads the posts the page links to, playlists' included")
        for category, count, feeds, ids, hint in (
                ("videos", 2, (), [VIDEO], other),
                ("videos", 2, empty, [VIDEO], other),
                ("playlists", 2, empty, [], playlists),
                ("videos", 0, empty, [], None)):
            with self.subTest(category=category, video_count=count, answered=bool(feeds)):
                ctx = fake(profile_page(video_count=count, body=link), feeds, routes=routes)
                posts, raised = listed(ctx, category)
                self.assertEqual([post.id for post in posts], ids)
                self.assertIs(type(raised) if raised else None, hint and Hidden)
                self.assertEqual(getattr(raised, "hint", None), hint)
                self.assertEqual(len(ctx.seen.requests), len(ids))

    def test_limit_holds_on_the_list_the_walled_fallback_and_a_read_again(self):
        """`--limit 3` takes 3 posts on each path, a photo post's photos counting once."""
        ids = [f"72345678901234567{n:02}" for n in range(6)]
        links = "".join(f'<a href="/@example_creator/video/{post}">clip</a>' for post in ids)
        routes = {f"{API}/embed/v2/{post}": Reply(embed_page(photo(post, f"{CDN}/{post}a",
                                                                   f"{CDN}/{post}b")))
                  for post in ids}
        page = profile_page(video_count=6, body=links)
        listing = {"hasMore": False, "itemList": [photo(post, f"{CDN}/{post}a", f"{CDN}/{post}b",
                                                        createTime=1700000000) for post in ids]}

        class Covered(FakeCtx):
            async def document(self):
                if self.seen.scrolls == 1:
                    return html.parse(CAPTCHA)
                return await super().document()

            async def handoff(self, message, *, until, timeout=None):  # noqa: ASYNC109
                return True

        for name, feeds, ctx in (("list", [(FEEDS[0][0], listing)], FakeCtx),
                                 ("walled", (), FakeCtx),
                                 ("read again", [Sent(FEEDS[0][0], listing, scroll=2)], Covered)):
            with self.subTest(name):
                saved = collect(TikTok, "tik:example_creator", limit=3,
                                ctx=fake(page, feeds, ctx=ctx, routes=routes),
                                categories=["videos"])
                self.assertEqual(len({found.post.id for found in saved}), 3)

    def test_a_block_stops_the_walled_fallback(self):
        def refused(request):
            raise Blocked("tik: blocked")

        other = "7234567890123456790"
        links = "".join(f'<a href="/@example_creator/video/{post}">clip</a>'
                        for post in (VIDEO, other))
        ctx = fake(profile_page(body=links), routes={
            f"{API}/embed/v2/{VIDEO}": refused,
            f"{API}/embed/v2/{other}": Reply(embed_page(EMBEDS[other][0]))})
        _, raised = listed(ctx, "videos")
        self.assertIs(type(raised), Blocked)
        # The scheduler's block rounds retried the first link; the second was never asked.
        self.assertEqual({url for _, url, _ in ctx.seen.requests}, {f"{API}/embed/v2/{VIDEO}"})

    def test_the_tabs(self):
        """S20: a tab that won't take the click fails its category with `Changed`."""
        class Stuck(FakeCtx):
            async def click(self, css, *, timeout=10.0):  # noqa: ASYNC109 (the kit's signature)
                raise NetworkError("nothing clickable in 10 s")

        page = profile_page("repost-tab", "liked-tab")
        for ctx, category, ids, line in (
                (fake(page, FEEDS[3:4]), "reposts", 4, None),
                (fake(page), "liked", 0, "liked: nothing public on this account"),
                (fake(page), "favorites", 0, "favorites: this profile has no such tab"),
                # In the markup but hidden by the page's CSS (live, 2026-11-19).
                (fake(page, shown=[]), "reposts", 0, "reposts: this profile has no such tab")):
            with self.subTest(category=category):
                posts, raised = listed(ctx, category)
                self.assertEqual((len(posts), raised), (ids, None))
                if line:
                    self.assertIn(("info", line), ctx.seen.logs)
        posts, raised = listed(fake(page, FEEDS[3:4], ctx=Stuck), "reposts")
        self.assertIsInstance(raised, Changed)

    def test_a_clicked_tab_is_read_to_its_end(self):
        feeds = [Sent(f"{API}/api/repost/item_list/?p=1", click=REPOSTS, body={
                      "hasMore": True, "itemList": [
                          video("7300000000000000010", f"{CDN}/r1", createTime=1700000000)]}),
                 Sent(f"{API}/api/repost/item_list/?p=2", scroll=1, body={
                      "hasMore": False, "itemList": [
                          video("7300000000000000011", f"{CDN}/r2", createTime=1700000001)]})]
        posts, _ = listed(fake(feeds=feeds), "reposts")
        self.assertEqual([post.id for post in posts],
                         ["7300000000000000010", "7300000000000000011"])

    def test_a_tab_ends_at_its_own_last_page(self):
        # A late answer heard with the last page isn't read past `hasMore: false`.
        feeds = [Sent(f"{API}/api/repost/item_list/?p={n}", click=REPOSTS, body={
                     "hasMore": n == 2, "itemList": [
                         video(f"730000000000000002{n}", f"{CDN}/r{n}", createTime=1700000000)]})
                 for n in (1, 2)]
        posts, _ = listed(fake(feeds=feeds), "reposts")
        self.assertEqual([post.id for post in posts], ["7300000000000000021"])

    def test_the_stories_are_read_once(self):
        ctx, profile = fake(feeds=FEEDS), Profile("example_creator")

        async def twice():
            return [[item async for item in profile.stories(ctx)] for _ in range(2)]

        self.assertEqual([len(items) for items in asyncio.run(twice())], [3, 0])


class ShortLinkTest(unittest.TestCase):
    def read(self, landed):
        item = video(VIDEO, f"{CDN}/e1", createTime=1700000000)
        ctx = FakeCtx(TikTok, "https://vm.tiktok.com/ZMabc123/",
                      pages={"https://vm.tiktok.com/ZMabc123": FakePage(url=landed)},
                      routes={f"{API}/embed/v2/{VIDEO}": Reply(embed_page(item))})
        return collect(TikTok, "https://vm.tiktok.com/ZMabc123/", ctx=ctx)

    def test_it_is_followed_to_its_video(self):
        for landed in (f"{API}/@example_creator/video/{VIDEO}?is_from_webapp=1",
                       f"{API}/@example_creator/photo/{VIDEO}"):
            with self.subTest(landed=landed):
                self.assertEqual([found.key for found in self.read(landed)], [VIDEO])

    def test_a_landing_that_is_no_post_is_refused_with_a_hint(self):
        with self.assertRaises(NotFound) as caught:
            self.read(f"{API}/@example_creator")
        self.assertTrue(caught.exception.hint)


class CaptchaTest(unittest.TestCase):
    def test_an_unsolved_captcha_is_held_and_the_page_opened_again(self):
        """It holds as the engine's block rounds do, and the rounds' end is `Blocked`."""
        class Captcha(FakeCtx):
            async def document(self):
                if len(self.seen.holds) < self.clears:
                    return html.parse(CAPTCHA)
                return await super().document()

        for clears, error in ((2, None), (9, Blocked)):
            with self.subTest(clears=clears):
                ctx = fake(profile_page(), ctx=Captcha)
                ctx.clears = clears
                _, raised = listed(ctx, "profile")
                self.assertIs(type(raised) if raised else None, error)
                self.assertEqual(len(ctx.seen.holds), min(clears, 3))
                self.assertEqual(ctx.seen.visited, [PROFILE] * (min(clears, 3) + 1))

    def test_captcha_words_in_a_script_are_no_captcha(self):
        page = profile_page(body="<script>var t = 'Verify to continue';</script>")
        ctx = fake(page)
        listed(ctx, "profile")
        self.assertEqual(ctx.seen.holds, [])

    def test_a_captcha_over_the_grid_is_waited_out_and_the_grid_read_again(self):
        class Covered(FakeCtx):
            """The grid is covered once scrolled, until a person solves the captcha."""

            solved = False

            async def document(self):
                if self.seen.scrolls and not self.solved:
                    return html.parse(CAPTCHA)
                return await super().document()

            async def handoff(self, message, *, until, timeout=None):  # noqa: ASYNC109
                self.solved = True
                return await until()

        # The first scroll brings nothing (the captcha); the list comes on the second.
        ctx = fake(feeds=[Sent(FEEDS[0][0], POSTS, scroll=2)], ctx=Covered)
        posts, raised = listed(ctx, "videos")
        self.assertEqual((len(posts), raised, ctx.seen.holds), (6, None, []))

    def test_a_list_that_stops_short_under_a_captcha_hands_off_and_reads_on(self):
        """Pages came, then the list went quiet under a captcha (in a hidden window the handoff
        shows it): it is solved, the list read on, and an item heard again is given once."""
        class Covered(FakeCtx):
            handed = 0

            async def document(self):
                if self.seen.scrolls and not self.handed:
                    return html.parse(CAPTCHA)
                return await super().document()

            async def handoff(self, message, *, until, timeout=None):  # noqa: ASYNC109
                self.handed += 1
                return await until()

        first = {"hasMore": True, "cursor": "1", "itemList": POSTS["itemList"][:1]}
        ctx = fake(profile_page(), [(FEEDS[0][0], first),
                                    Sent(FEEDS[0][0], {**POSTS, "itemList": POSTS["itemList"][:2]},
                                         scroll=2)], ctx=Covered)
        posts, raised = listed(ctx, "videos")
        self.assertEqual(([post.id for post in posts], raised, ctx.handed),
                         (["7300000000000000001"], None, 1))
        self.assertIn(("info", "videos: 2 found; the profile lists 2"), ctx.seen.logs)


class EmptyPageTest(unittest.TestCase):
    """An empty answer that still says `hasMore`, on the scrolled post list."""

    LIST = f"{API}/api/post/item_list/?count=2"

    def ids(self, *ids, more, cursor=None):
        body = {"hasMore": more, "itemList": [video(i, f"{CDN}/{i}", createTime=1700000000)
                                              for i in ids]}
        return {**body, "cursor": cursor} if cursor else body

    def read(self, *answers):
        ctx = fake(feeds=[Sent(self.LIST, answer, scroll=n or None)
                          for n, answer in enumerate(answers)])
        posts, raised = listed(ctx, "videos")
        return [post.id for post in posts], raised, ctx

    def test_an_empty_page_that_says_hasmore_is_held_once_then_ends(self):
        empty = self.ids(more=True, cursor="9")
        got, raised, ctx = self.read(self.ids("1", "2", more=True, cursor="2"), empty, empty,
                                     self.ids("9", more=False))
        self.assertEqual((got, raised), (["1", "2"], None))
        self.assertEqual(len(ctx.seen.holds), 1)
        self.assertTrue([log for log in ctx.seen.logs if log[0] == "warn"])

    def test_a_page_that_comes_full_after_the_hold_carries_on(self):
        got, raised, ctx = self.read(self.ids("1", "2", more=True, cursor="2"),
                                     self.ids(more=True, cursor="9"), self.ids("3", more=False))
        self.assertEqual((got, raised, len(ctx.seen.holds)), (["1", "2", "3"], None, 1))


class BestVideoTest(unittest.TestCase):
    def test_the_best_whole_mp4_with_its_mirrors(self):
        def entry(url, width, height, bitrate, **marks):
            play = {"Width": width, "Height": height, "UrlKey": marks.pop("UrlKey", "v0_h264"),
                    "UrlList": [url] if isinstance(url, str) else url}
            return {"Bitrate": bitrate, "GearName": "normal", "Format": "mp4", "PlayAddr": play,
                    **marks}

        for given, expected in [
            ({"playAddr": {"urlList": ["https://v/a.mp4", "https://v/b.mp4", ""]}},
             ("https://v/a.mp4", "https://v/b.mp4")),
            ({"bitrateInfo": [entry(["https://v16/a", "https://v19/a"], 1, 1, 9)]},
             ("https://v16/a", "https://v19/a")),
            # By pixels, then bitrate, over playAddr; the watermarked copy never.
            ({"playAddr": "https://v/play", "downloadAddr": "https://v/wm", "bitrateInfo": [
                entry("https://v/720", 720, 1280, 2), entry("https://v/1080-low", 1080, 1920, 1),
                entry("https://v/1080-high", 1080, 1920, 3), entry("https://v/540", 540, 960, 9),
            ]}, ("https://v/1080-high",)),
            *[({"bitrateInfo": [entry("https://v/720", 720, 1280, 1),
                                entry(url, 1080, 1920, 5, **marks)]}, ("https://v/720",))
              for url, marks in (("https://v/1080", {"Format": "dash"}),
                                 ("https://v/1080", {"GearName": "adapt_1080_dash"}),
                                 ("https://v/1080", {"UrlKey": "v0_bytevc1_1080p_dash"}),
                                 ("https://v/1080/manifest.mpd", {}))],
            ({"playAddr": "https://v/play", "bitrateInfo": [
                entry("https://v/dash", 1080, 1920, 1, Format="dash")]}, ("https://v/play",)),
            ({"downloadAddr": "https://v/wm"}, None),
            ({}, None),
        ]:
            with self.subTest(given=given):
                self.assertEqual(best_video(given), expected)


class AntiBanTest(unittest.TestCase):
    def test_pace_user_agent_and_lock_walls_and_status_codes(self):
        from flnd.errors import LoginError

        self.assertGreaterEqual(TikTok.pace, 1.5)
        self.assertIsNone(TikTok.user_agent)
        for wall_path in ("/verify", "/account/suspended"):
            with self.subTest(wall=wall_path):
                ctx = FakeCtx(TikTok, "tik:example_creator", pages={
                    PROFILE: FakePage(url=f"{API}{wall_path}"),
                })
                with self.assertRaises(LoginError) as caught:
                    collect(TikTok, "tik:example_creator", ctx=ctx, categories=["videos"])
                self.assertFalse(caught.exception.retryable)

        for code, exc_type in ((8, LoginError), (2483, LoginError),
                               (10000, Blocked), (10201, Blocked)):
            with self.subTest(code=code):
                ctx = FakeCtx(TikTok, "tik:example_creator", pages={
                    PROFILE: FakePage(
                        feeds=[Sent(f"{API}/api/post/item_list/", {"status_code": code})],
                    ),
                })
                with self.assertRaises(exc_type) as caught:
                    collect(TikTok, "tik:example_creator", ctx=ctx, categories=["videos"])
                self.assertFalse(caught.exception.retryable)

    def test_no_progress_429_reload_aborts_and_reruns_wait_out(self):
        from unittest import mock

        queue = [
            FakePage(feeds=[
                Sent(f"{API}/api/post/item_list/", {"itemList": [video("1", f"{CDN}/1.mp4")],
                                                    "hasMore": True}),
                Sent(f"{API}/api/post/item_list/", status=429, scroll=1),
            ]),
            FakePage(feeds=[
                Sent(f"{API}/api/post/item_list/", status=429),
            ]),
        ]
        ctx = FakeCtx(TikTok, "tik:example_creator", pages={})
        orig_open = ctx.open

        async def async_open(target_url):
            if target_url == PROFILE:
                ctx._pages[target_url] = queue.pop(0)
            return await orig_open(target_url)

        with mock.patch.object(ctx, "open", side_effect=async_open):
            with self.assertRaises(Blocked) as caught:
                collect(TikTok, "tik:example_creator", ctx=ctx, categories=["videos"])
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(ctx.seen.holds), 1)

    def test_empty_hasmore_stall_marks_listing_stalled_and_pace_is_conservative(self):
        from flnd.engine.ctx import Listing

        self.assertGreaterEqual(TikTok.pace, 2.0)
        empty = {"hasMore": True, "cursor": "9", "itemList": []}
        full = {"hasMore": True, "cursor": "2",
                "itemList": [video("1", f"{CDN}/1", createTime=1700000000)]}
        ctx = fake(feeds=[Sent(f"{API}/api/post/item_list/?count=2", full),
                          Sent(f"{API}/api/post/item_list/?count=2", empty, scroll=1),
                          Sent(f"{API}/api/post/item_list/?count=2", empty, scroll=2)])
        ctx.listing = Listing(None)
        posts, raised = listed(ctx, "videos")
        self.assertIsNone(raised)
        self.assertEqual([p.id for p in posts], ["1"])
        self.assertTrue(ctx.listing.stalled)


if __name__ == "__main__":
    unittest.main()
