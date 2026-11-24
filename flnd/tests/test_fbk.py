"""Facebook on plugin API 2 (`src/plugins/fbk.py`), run on `FakeCtx`: the routes, a profile's
categories, blocks and walls, pages that don't load, the single links and the media walker."""

import asyncio
import datetime
import json
import unittest
from types import SimpleNamespace

import fakes  # noqa: F401  (refuses real profiles; fails a coroutine never awaited)

from flnd.engine.resolve import Manifest, resolve
from flnd.errors import Blocked, LoginError, NotFound, SiteError, UsageError
from flnd.plugin import Plugin, Range, Target
from flnd.plugins import _manifest
from flnd.plugins.fbk import (
    HIGHLIGHT_NEXT,
    TEMPORARILY_BLOCKED,
    Facebook,
    media_items,
    next_page,
    profile_of,
    tab_cursor,
)
from flnd.testing import FakeCtx, FakePage, Reply, Sent, check, collect

SITE = "https://www.facebook.com"
GRAPHQL = f"{SITE}/api/graphql/"
NOON = 1700049600
DAY = "2023-11-15"
USER, VIDEO, PHOTO, POST, WATCH, SHARE = (
    "user", "video", "photo", "post", "watch", "share")

# Every form fbk reads, and what it becomes: (kind, value), a refusal's text, or None (no
# route reads it). The kinds and values are the history's keys: a change here is a migration.
ROUTES = [
    ("fbk:example.page", (USER, "example.page")),
    ("fbk:@example.page", (USER, "example.page")),
    ("fbk:100012345678901", (USER, "100012345678901")),
    ("fbk:facebook.com/example.page", (USER, "example.page")),
    ("https://www.facebook.com/example.page", (USER, "example.page")),
    ("https://facebook.com/example.page/", (USER, "example.page")),
    ("https://fb.com/@example.page", (USER, "example.page")),
    ("https://m.facebook.com/example.page/photos", (USER, "example.page")),
    ("https://www.facebook.com/example.page/photos_by", (USER, "example.page")),
    ("https://www.facebook.com/example.page/photos/a.1/2", (USER, "example.page")),
    ("https://www.facebook.com/example.page/about", (USER, "example.page")),
    ("https://www.facebook.com/watchman", (USER, "watchman")),
    ("https://www.facebook.com/profile.php?id=100012345678901", (USER, "100012345678901")),
    ("https://fb.com/people/example/100012345678901", (USER, "100012345678901")),
    ("https://www.facebook.com/profile.php?id=abc", None),
    ("https://www.facebook.com/people/example", None),
    ("https://www.facebook.com/profile.php", None),
    ("https://www.facebook.com/", None),
    ("fbk:example.com/example.page", None),
    ("fbk:not a name", None),
    ("https://www.facebook.com/example.page/posts/123", (POST, "post/example.page_123")),
    (
        "https://m.facebook.com/example.page/posts/pfbid0Abc/?x=1",
        (POST, "post/example.page_pfbid0Abc"),
    ),
    ("https://fb.com/100012345678901/posts/7", (POST, "post/100012345678901_7")),
    ("fbk:post/example.page_123", (POST, "post/example.page_123")),
    ("fbk:post/100012345678901_pfbid0Abc", (POST, "post/100012345678901_pfbid0Abc")),
    ("https://www.facebook.com/example.page/videos/some-title/22/", (VIDEO, "video/22")),
    ("https://fb.com/example.page/videos/22", (VIDEO, "video/22")),
    ("https://www.facebook.com/share/p/1aBc-9/", (SHARE, "share/p/1aBc-9")),
    ("facebook.com/share/r/1aBc", (SHARE, "share/r/1aBc")),
    ("https://fb.com/share/v/1aBc", (SHARE, "share/v/1aBc")),
    ("https://m.facebook.com/watch/?v=5", (VIDEO, "video/5")),
    ("https://web.facebook.com/reel/6", (VIDEO, "video/6")),
    ("https://mbasic.facebook.com/photo.php?fbid=7", (PHOTO, "photo/7")),
    ("https://mbasic.facebook.com/example.page", (USER, "example.page")),
    ("https://m.facebook.com/share/r/1aBc", (SHARE, "share/r/1aBc")),
    ("fbk:me", (USER, "me")),
    ("https://www.facebook.com/share/x/1aBc", None),
    ("https://fb.com/example.page/posts", (USER, "example.page")),
    ("https://fb.com/photos/1", "photo lists aren't supported"),
    ("https://www.facebook.com/groups/x", "groups aren't supported"),
    ("https://fb.com/events/x", "events aren't supported"),
    ("https://www.facebook.com/stories/x/1", "stories aren't supported"),
    ("https://m.facebook.com/marketplace/item/1", "marketplace pages aren't supported"),
    ("https://www.facebook.com/hashtag/cats", "hashtag pages aren't supported"),
    ("https://www.facebook.com/watch/", "watch pages without v= aren't supported"),
    ("https://www.facebook.com/watch/?ref=x", "watch pages without v= aren't supported"),
    ("https://www.facebook.com/watch/?v=123", (VIDEO, "video/123")),
    ("fbk:facebook.com/watch/?v=1", (VIDEO, "video/1")),
    ("https://fb.com/example.page/videos/2", (VIDEO, "video/2")),
    ("https://fb.com/reel/1", (VIDEO, "video/1")),
    ("https://www.facebook.com/reels/123", (VIDEO, "video/123")),
    ("https://www.facebook.com/videos/123", (VIDEO, "video/123")),
    ("https://fb.com/photo/?fbid=1", (PHOTO, "photo/1")),
    ("https://www.facebook.com/photo.php?fbid=123&set=a.4&type=3", (PHOTO, "photo/123")),
    ("https://www.facebook.com/permalink.php?story_fbid=pfbid0Abc&id=100012345678901",
     (POST, "post/100012345678901_pfbid0Abc")),
    ("https://www.facebook.com/story.php?story_fbid=7&id=42", (POST, "post/42_7")),
    ("https://fb.watch/aBc-12/", (WATCH, "watch/aBc-12")),
    ("fb.watch/aBc-12", (WATCH, "watch/aBc-12")),
    ("https://www.facebook.com/photo.php", "a photo.php link names its photo by fbid="),
    ("https://www.facebook.com/photo/", "a photo link names its photo by fbid="),
    ("https://www.facebook.com/permalink.php?story_fbid=1", "a permalink.php link names"),
    ("https://www.facebook.com/story.php?id=1", "a story.php link names its post"),
    ("fbk:photo.php", "'photo.php' is a page of Facebook"),
    ("fbk:permalink.php", "'permalink.php' is a page of Facebook"),
]


def representation(id, mime, url, *, height=0, bandwidth=1000):
    size = f' width="{height * 9 // 16}" height="{height}"' if height else ""
    return (f'<Representation id="{id}" mimeType="{mime}"{size} bandwidth="{bandwidth}">'
            f'<BaseURL>{url}</BaseURL><SegmentBase indexRange="1-2"/></Representation>')


def manifest(videos, audios):
    return ('<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"><Period>'
            f'<AdaptationSet id="0">{"".join(videos)}</AdaptationSet>'
            f'<AdaptationSet id="1">{"".join(audios)}</AdaptationSet></Period></MPD>')


DASH = manifest(
    [representation("1", "video/mp4", "https://cdn.example/360.mp4", height=640),
     representation("2", "video/mp4", "https://cdn.example/1080.mp4", height=1920)],
    [representation("3", "audio/mp4", "https://cdn.example/a64.mp4", bandwidth=64),
     representation("4", "audio/mp4", "https://cdn.example/a95.mp4", bandwidth=95)])
WEBM = manifest([representation("v", "video/webm", "https://cdn.example/v.webm", height=720)],
                [representation("a", "audio/webm", "https://cdn.example/a.webm")])


def embedded(*results, extra=""):
    """A page embedding `results` in `__bbox` blocks, as Comet does."""
    blocks = "".join(
        '<script type="application/json">'
        f'{json.dumps({"require": [["x", {"__bbox": {"result": result}}]]})}</script>'
        for result in results)
    return f"<html><body>{extra}{blocks}</body></html>"


def page(*results, feeds=(), scroll=(), url=""):
    """A page embedding `results`: `feeds` come as it loads, `scroll[n]` on its n+1-th scroll."""
    return FakePage(embedded(*results), url=url,
                    feeds=[*[(GRAPHQL, line) for line in feeds],
                           *[Sent(GRAPHQL, line, scroll=n) for n, batch in enumerate(scroll, 1)
                             for line in batch]])


def header(*tab_keys, user_id=None, cover="https://cdn.example/c.png", typename="User"):
    sections = [{"node": {"tab_key": key, "all_collections": {"nodes": []}}} for key in tab_keys]
    return {"data": {"user": {"profile_header_renderer": {"user": {
        "id": user_id, "__typename": typename,
        "profilePicLarge": {"uri": "https://cdn.example/v/pic.jpg?stp=1"},
        "profilePhoto": {"id": "11"},
        "cover_photo": {"photo": {"id": "12", "image": {"uri": cover}}},
        "story_bucket": {"nodes": [{"id": "900"}]},
        "profile_tabs": {"profile_user": {"timeline_nav_app_sections": {"edges": sections}}},
    }}}}}


def photo(photo_id, uri="https://cdn.example/p.jpg", *, stamp=NOON, album=None, owner=None):
    node = {"__typename": "Photo", "id": photo_id, "viewer_image": {"uri": uri}}
    if stamp:
        node["creation_time"] = stamp
    if album:
        node["album"] = {"name": album}
    if owner:
        node["owner"] = {"id": owner}
    return {"data": {"node": node}}


def video_node(video_id, *, stamp=NOON, dash=None):
    progressive = [{"progressive_url": "https://cdn.example/sd.mp4", "metadata": {"quality": "SD"}},
                   {"progressive_url": "https://cdn.example/hd.mp4", "metadata": {"quality": "HD"}}]
    result = {"progressive_urls": progressive}
    if dash:
        result["dash_manifests"] = [{"manifest_xml": dash}]
    return {"__typename": "Video", "id": video_id, "publish_time": stamp,
            "videoDeliveryResponseFragment": {"videoDeliveryResponseResult": result},
            "playable_url": "https://cdn.example/sd.mp4"}


def video(video_id, **given):
    return {"data": {"node": video_node(video_id, **given)}}


def info(cursor):
    """A streamed line with the timeline's `page_info`; "" for the last page."""
    return {"data": {"page_info": {"has_next_page": bool(cursor), "end_cursor": cursor or None}}}


BASE = f"{SITE}/example.page"
ALBUMS = {"Trip": f"{SITE}/media/set/?set=a.5&type=3", "Home: 1": f"{SITE}/media/set/?set=a.6"}
TILES = {"data": {"node": {"edges": [{"node": {
    "__typename": "TimelineAppCollectionItem", "url": url, "title": {"text": title},
    "node": {"__typename": "Album"}}} for title, url in ALBUMS.items()]}}}
BUCKET = {"data": {"bucket": {"unified_stories_with_notes": {"edges": [
    {"node": {"creation_time": str(NOON), "attachments": [{"media": video_node("31")}]}},
    {"node": {"creation_time": str(NOON), "attachments": [{"media": {
        "__typename": "Photo", "id": "32", "image": {"uri": "https://cdn.example/s.png"}}}]}},
    {"node": {"attachments": [{"media": {"__typename": "Photo", "id": "33",
                                         "image": {"uri": "https://cdn.example/s.jpg"}}}]}},
]}}}}
TABS = ("photos_by", "photos_of", "photos_albums", "reels_tab", "videos_by")
# A profile with every kind of file fbk saves. The profile page's first load and its scroll are
# both read by `posts`, and both are filtered by the header's owner id.
PAGES = {
    BASE: page(header(*TABS, user_id="42"), photo("51"), video("52"), photo("53", stamp=0),
               photo("54", "https://cdn.example/w.png?stp=dst-webp"),
               photo("55", owner="999"), feeds=[photo("56", owner="999")],
               scroll=[[photo("57", owner="999"), photo("58", owner="42")]]),
    f"{BASE}/photos_by": page(photo("61", album="Summer: 2026"), photo("62"),
                              scroll=[[photo("63"), photo("64", owner="999"), video("65")]]),
    f"{BASE}/photos_of": page(photo("71")),
    f"{BASE}/photos_albums": page(TILES, photo("81")),
    ALBUMS["Trip"]: page(photo("91"), photo("62", "https://cdn.example/dup.jpg"),
                         photo("93", owner="999")),
    ALBUMS["Home: 1"]: page(photo("92", stamp=0)),
    f"{BASE}/reels_tab": page(video("22", dash=DASH), scroll=[[video("24"), video("22")]]),
    f"{BASE}/videos_by": page(video("23"), scroll=[[photo("66")]]),
    f"{SITE}/stories/900/": page(BUCKET),
}
# What the API 1 source saved from the same pages, as (path, key): the keys are unchanged.
PAIRS = [
    ("albums/2023-11-15_81.jpg", "81"), ("albums/Home_1/92.jpg", "92"),
    ("albums/Summer_2026/2023-11-15_61.jpg", "61"), ("albums/Trip/2023-11-15_91.jpg", "91"),
    ("photos/2023-11-15_62.jpg", "62"), ("photos/2023-11-15_63.jpg", "63"),
    ("posts/2023-11-15_51.jpg", "51"), ("posts/2023-11-15_58.jpg", "58"), ("posts/53.jpg", "53"),
    ("profile/cover_12.png", "profile/cover_12.png"),
    ("profile/profile_11.jpg", "profile/profile_11.jpg"),
    ("reels/2023-11-15_22.mp4", "22"), ("reels/2023-11-15_24.mp4", "24"),
    ("stories/2023-11-15_31.mp4", "31"), ("stories/2023-11-15_32.png", "32"),
    ("stories/33.jpg", "33"),
    ("tagged/2023-11-15_71.jpg", "71"), ("videos/2023-11-15_23.mp4", "23"),
    ("videos/2023-11-15_52.mp4", "52"), ("videos/2023-11-15_65.mp4", "65"),
    ("videos/2023-11-15_66.jpg", "66"),
]


# The photo page the `date` hook asks for an undated post's day, holding none.
UNDATED = {f"{SITE}/photo/": Reply("<p>no date</p>")}


def run(text="fbk:example.page", categories=None, **given):
    """What a download of `text` would save, and the fake it ran on."""
    ctx = FakeCtx(Facebook, **{"pages": PAGES, "routes": UNDATED, **given})
    return collect(Facebook, text, ctx=ctx, categories=categories), ctx


class RoutesTest(unittest.TestCase):
    def test_every_form(self):
        for text, expected in ROUTES:
            with self.subTest(text=text):
                if isinstance(expected, str):
                    with self.assertRaisesRegex(UsageError, f"^fbk: {expected}"):
                        Facebook.resolve(text)
                    continue
                found = Facebook.resolve(text)
                self.assertEqual(found, expected and Target(f"fbk:{expected[1]}",
                                                            kind=expected[0]))

    def test_the_examples_resolve_and_the_class_checks(self):
        self.assertEqual([str(found) for found in check(Facebook)], ["fbk:example.page"])


class ProfileTest(unittest.TestCase):
    def test_every_category_saves_the_paths_and_keys_of_api_1(self):
        saved, _ = run(categories=["all"])
        # 54's CDN sends WebP (`stp=dst-webp` on a .png), as API 1 named it: the engine names it
        # from the content, which `collect` doesn't see, so its stem and key are checked.
        [webp] = [s for s in saved if s.key == "54"]
        self.assertEqual((webp.path.rpartition(".")[0], webp.file.name),
                         ("posts/2023-11-15_54", None))
        self.assertEqual(sorted((s.path, s.key) for s in saved if s is not webp), PAIRS)
        self.assertEqual({tuple(s.post.headers.items()) for s in saved},
                         {(("Referer", f"{SITE}/"),)})

    def test_posts_pops_results_from_profile(self):
        profile = {"results": [{"data": photo("1")["data"]}], "owner": "42"}
        ctx = FakeCtx(Facebook)

        async def run_posts():
            return [p async for p in Facebook().posts(ctx, profile)]

        posts = asyncio.run(run_posts())
        self.assertEqual(len(posts), 1)
        self.assertNotIn("results", profile)

    def test_each_category_opens_only_its_pages(self):
        cases = [
            # 55, 56 and 57 are another account's: left out on both initial load and scroll.
            (["posts"], [BASE], ["51", "52", "53", "54", "58"]),
            (["tagged"], [BASE, f"{BASE}/photos_of"], ["71"]),
            (["albums"], [BASE, f"{BASE}/photos_albums", ALBUMS["Trip"], ALBUMS["Home: 1"]],
             ["81", "91", "62", "92"]),  # 93 is another account's
            (["reels"], [BASE, f"{BASE}/reels_tab"], ["22", "24"]),
            (["stories", "profile"], [BASE, f"{SITE}/stories/900/"], None),
            # `tagged` is other people's content: taken only when named, or with /all.
            (None, [BASE, f"{BASE}/photos_by", f"{BASE}/photos_albums", ALBUMS["Trip"],
                    ALBUMS["Home: 1"], f"{BASE}/reels_tab", f"{BASE}/videos_by",
                    f"{SITE}/stories/900/"], None),
        ]
        for categories, visited, keys in cases:
            with self.subTest(categories=categories):
                saved, ctx = run(categories=categories)
                self.assertEqual(ctx.seen.visited, visited)
                if keys is not None:
                    self.assertEqual([s.key for s in saved], keys)

    def test_album_pages_and_story_buckets_load_in_warm_tabs_first(self):
        cases = [(["albums"], [ALBUMS["Trip"], ALBUMS["Home: 1"]]),
                 (["stories"], [f"{SITE}/stories/900/"]), (["posts"], [])]
        for categories, warmed in cases:
            with self.subTest(categories=categories):
                _, ctx = run(categories=categories)
                self.assertEqual(ctx.seen.warmed, warmed)

    def test_a_tab_the_header_lacks_is_skipped_and_without_a_header_each_is_tried(self):
        _, ctx = run(categories=["tagged"], pages={BASE: page(header("photos_by"))})
        self.assertEqual(ctx.seen.visited, [BASE])
        self.assertIn(("info", "tagged: this profile has no such tab"), ctx.seen.logs)
        pages = {BASE: page(photo("1")), f"{BASE}/videos_by": page(video("2"))}
        saved, ctx = run(categories=["videos", "stories", "profile"], pages=pages)
        self.assertEqual(ctx.seen.visited, [BASE, f"{BASE}/videos_by"])
        self.assertEqual([s.key for s in saved], ["2"])
        self.assertIn(("warn", "the profile header wasn't found; its stories are skipped"),
                      ctx.seen.logs)

    def test_the_route_gives_each_tabs_url_for_its_warm_tab(self):
        route = next(known for known in Facebook.routes if known.kind == USER)
        made = {category.name: category for category in route.categories}
        shown = profile_of(BASE, [header("photos", "reels_tab")], [])
        self.assertEqual({name: made[name].link(shown) for name in made}, {
            "posts": "", "highlights": "", "photos": f"{BASE}/photos", "tagged": "", "albums": "",
            "reels": f"{BASE}/reels_tab", "videos": "", "stories": "", "profile": ""})

    def test_a_pictures_name_and_key_take_the_format_the_cdn_sends(self):
        # The key is the path, so the extension is pinned as API 1 named it, not left to the
        # engine's reading of the content.
        cases = [("https://cdn.example/c.png?stp=dst-jpg_s206x206&x=1", "jpg"),
                 ("https://cdn.example/c.png?stp=dst-webp", "webp"),
                 ("https://cdn.example/c.webp", "webp"), ("https://cdn.example/c", "jpg")]
        for url, extension in cases:
            with self.subTest(url=url):
                pages = {BASE: page(header(cover=url))}
                saved, _ = run(categories=["profile"], pages=pages)
                self.assertEqual([(s.path, s.key) for s in saved],
                                 [("profile/profile_11.jpg",) * 2,
                                  (f"profile/cover_12.{extension}",) * 2])

    def test_when_keeps_undated_files_and_drops_files_outside_it(self):
        pages = {BASE: page(header(), photo("1"), photo("2", stamp=0))}
        saved, _ = run(categories=["posts"], pages=pages,
                       when=Range(since=datetime.date(2023, 11, 16)))
        self.assertEqual([s.key for s in saved], ["2"])


class BlockTest(unittest.TestCase):
    def test_the_block_page_is_held_by_the_block_rules(self):
        block = "<html><body>You've been temporarily blocked</body></html>"
        cases = [
            # The engine's `blocked` reads the main document: no `__bbox`, the block text.
            ("main document", block, f"fbk: blocked, HTTP 200 for {BASE}"),
            # A page with boxes but no results, whose text says it: the plugin holds itself.
            ("page text", embedded({"x": 1}, extra="temporarily blocked"), TEMPORARILY_BLOCKED),
        ]
        for label, html, reason in cases:
            with self.subTest(label):
                ctx = FakeCtx(Facebook, pages={BASE: FakePage(html)})
                with self.assertRaises(Blocked):
                    collect(Facebook, "fbk:example.page", ctx=ctx, categories=["posts"])
                self.assertEqual({hold[0] for hold in ctx.seen.holds}, {reason})
        self.assertEqual(Facebook().blocked(SimpleNamespace(text=block)), 0.0)
        self.assertIsNone(Facebook().blocked(SimpleNamespace(text=embedded(photo("1"),
                                                                          extra=block))))

    def test_a_comet_error_line_is_held_and_the_scroll_goes_on(self):
        comet = {"__dr": "CometErrorRoot.react"}
        for category, url in (("photos", f"{BASE}/photos_by"), ("posts", BASE)):
            with self.subTest(category):
                pages = {BASE: page(header(*TABS)),
                         url: page(header(*TABS), photo("1"),
                                   scroll=[[photo("2"), comet, photo("3")]])}
                saved, ctx = run(categories=[category], pages=pages)
                self.assertEqual([s.key for s in saved], ["1", "2", "3"])
                self.assertEqual([hold[0] for hold in ctx.seen.holds], [TEMPORARILY_BLOCKED])
                pages[url] = page(header(*TABS), photo("1"), scroll=[[comet] * 4])
                with self.assertRaises(Blocked):
                    run(categories=[category], pages=pages)

    def test_a_rate_limit_code_line_is_held_and_the_scroll_goes_on(self):
        limited = {"errors": [{"code": 1675004, "message": "rate limited"}]}
        pages = {BASE: page(header(*TABS)),
                 f"{BASE}/photos_by": page(header(*TABS), photo("1"),
                                           scroll=[[photo("2"), limited, photo("3")]])}
        saved, ctx = run(categories=["photos"], pages=pages)
        self.assertEqual([s.key for s in saved], ["1", "2", "3"])
        self.assertEqual([hold[0] for hold in ctx.seen.holds], [TEMPORARILY_BLOCKED])

    def test_a_scroll_stops_where_the_tab_says_it_ends(self):
        # The Tiles feed's "no next page" as the page loads says nothing; the tab's own last
        # page, after one with a next page, ends the scroll: the third scroll never happens.
        scroll = [[photo("2"), info("c1")], [photo("3"), info("")], [photo("4")]]
        for category, url in (("photos", f"{BASE}/photos_by"), ("posts", BASE)):
            with self.subTest(category):
                pages = {BASE: page(header(*TABS)),
                         url: page(header(*TABS), photo("1"), feeds=[info("")], scroll=scroll)}
                saved, ctx = run(categories=[category], pages=pages)
                self.assertEqual([s.key for s in saved], ["1", "2", "3"])
                self.assertEqual(ctx.seen.scrolls, 2)

    def test_the_login_page_is_a_wall_and_a_name_like_it_is_not(self):
        with self.assertRaises(LoginError):
            run(categories=["posts"], pages={BASE: page(url=f"{SITE}/login/?next=x")})
        saved, _ = run("fbk:login.example", categories=["posts"],
                       pages={f"{SITE}/login.example": page(header(), photo("1"))})
        self.assertEqual([s.key for s in saved], ["1"])


class LoadTest(unittest.TestCase):
    """S4 and S18: a page that doesn't load fails its category, never reads as empty."""

    def test_a_page_that_fails_or_lands_elsewhere_fails_the_target(self):
        elsewhere = FakePage("<html></html>", url=f"{SITE}/")
        cases = [
            ("profile", BASE, ["posts"]), ("tab", f"{BASE}/photos_by", ["photos"]),
            ("album", ALBUMS["Trip"], ["albums"]),
            ("story bucket", f"{SITE}/stories/900/", ["stories"]),
        ]
        for label, url, categories in cases:
            for shown, error in ((FakePage(status=404), NotFound), (elsewhere, SiteError)):
                with self.subTest(label, status=shown.status), self.assertRaises(error):
                    run(categories=categories, pages={**PAGES, url: shown})

    def test_an_empty_tab_on_its_own_page_is_empty(self):
        saved, _ = run(categories=["photos"],
                       pages={**PAGES, f"{BASE}/photos_by": FakePage("<html></html>")})
        self.assertEqual(saved, [])


class SingleLinkTest(unittest.TestCase):
    def test_each_link_keeps_the_media_it_names(self):
        short = "https://fb.watch/aBc-12/"
        watch = f"{SITE}/watch/?v=22"
        permalink = f"{SITE}/permalink.php?story_fbid=7&id=42"
        cases = [
            (f"{BASE}/videos/22/", {watch: page(video("22"), video("99"))}, [watch], ["22"]),
            (short, {short: FakePage(url=f"{SITE}/reel/22"), watch: page(video("22"))},
             [short, watch], ["22"]),
            (short, {short: FakePage(url=f"{BASE}/videos/some-title/22/"),
                     watch: page(video("22"))}, [short, watch], ["22"]),
            (f"{SITE}/photo.php?fbid=21&set=a.4", {f"{SITE}/photo/?fbid=21": page(
                photo("21"), photo("5"))}, [f"{SITE}/photo/?fbid=21"], ["21"]),
            (permalink, {permalink: page(photo("1", owner="42"), photo("2", owner="999"),
                                         video("3"))}, [permalink], ["1", "3"]),
        ]
        for text, pages, visited, keys in cases:
            with self.subTest(text=text):
                saved, ctx = run(text, pages=pages)
                self.assertEqual(ctx.seen.visited, visited)
                self.assertEqual([s.key for s in saved], keys)
                # A link whose data names an owner is filed by its category.
                self.assertEqual({s.post.fields["part"] for s in saved},
                                 {"posts"} if text == permalink else {""})

    def test_a_link_is_filed_by_its_owner_and_the_kind_the_data_gives(self):
        def owned(result, kind=None):
            node = result["data"]["node"]
            node.update(owner={"id": "42", "vanity": "example.page"})
            node.update(__typename=kind or node["__typename"])
            return result

        watch, fbid = f"{SITE}/watch/?v=22", f"{SITE}/photo/?fbid=21"
        permalink = f"{SITE}/permalink.php?story_fbid=7&id=42"
        rows = [(f"{BASE}/videos/22/", {watch: page(owned(video("22")))}, "example.page/videos"),
                (f"{SITE}/reel/22", {watch: page(owned(video("22"), "Reel"))},
                 "example.page/reels"),
                (f"{SITE}/photo.php?fbid=21", {fbid: page(owned(photo("21")))},
                 "example.page/photos"),
                (permalink, {permalink: page(owned(photo("1")))}, "example.page/posts"),
                (f"{SITE}/reel/22", {watch: page(video("22"))}, "22")]
        for text, pages, folder in rows:
            with self.subTest(text=text, folder=folder):
                [found], _ = run(text, pages=pages)
                self.assertEqual(fakes.filed(Facebook, text, found), folder)

    def test_a_link_to_nothing_is_not_found(self):
        short = "https://fb.watch/aBc-12/"
        with self.assertRaisesRegex(NotFound, "didn't lead to a video"):
            run(short, pages={short: FakePage(url=BASE)})
        with self.assertRaisesRegex(NotFound, "shows no photo or video"):
            run(f"{SITE}/photo.php?fbid=21", pages={f"{SITE}/photo/?fbid=21": page(photo("5"))})
        with self.assertRaises(LoginError):
            run(short, pages={short: FakePage(url=f"{SITE}/login/?next=x")})

    def test_a_video_is_named_by_the_templates_from_its_best_streams(self):
        hd_first = ("https://cdn.example/hd.mp4", "https://cdn.example/sd.mp4")
        cases = [(video("22", dash=DASH), ("22/2023-11-15_22.mp4", "22"),
                  ("https://cdn.example/1080.mp4", (), "https://cdn.example/a95.mp4")),
                 (video("22", stamp=0, dash=DASH), ("22/22.mp4", "22"), None),
                 (video("22", dash=WEBM), ("22/2023-11-15_22.webm", "22"), None),
                 # No manifest: every progressive address is a mirror, HD first.
                 (video("22"), ("22/2023-11-15_22.mp4", "22"), (None, hd_first, None))]
        for result, pair, streams in cases:
            with self.subTest(pair=pair, streams=streams):
                [saved], _ = run(f"{SITE}/watch/?v=22",
                                 pages={f"{SITE}/watch/?v=22": page(result)})
                self.assertEqual((saved.path, saved.key), pair)
                if streams:
                    self.assertEqual((saved.file.url, saved.file.mirrors, saved.file.audio),
                                     streams)


def owned_by_name(result, vanity="example.page", ident="42"):
    """`result`'s node, owned by the account `vanity` (id `ident`)."""
    result["data"]["node"]["owner"] = {"id": ident, "vanity": vanity}
    return result


class HardTargetTest(unittest.TestCase):
    def test_a_typed_key_resolves_to_itself(self):
        manifest = Manifest(_manifest.PLUGINS)
        for link in [
            "https://www.facebook.com/example.page/posts/pfbid0Abc",
            "https://www.facebook.com/100012345678901/posts/7",
            "https://www.facebook.com/permalink.php?story_fbid=pfbid0Abc&id=100012345678901",
            "https://www.facebook.com/example.page/videos/some-title/22",
            "https://web.facebook.com/reel/6", "https://mbasic.facebook.com/photo.php?fbid=7",
            "https://www.facebook.com/share/p/1aBc", "https://www.facebook.com/share/r/1aBc",
            "https://www.facebook.com/share/v/1aBc", "https://fb.watch/aBc-12/",
        ]:
            with self.subTest(link=link):
                key = resolve(link, manifest=manifest, known={}).key
                self.assertTrue(key.startswith("fbk:"))
                self.assertEqual(resolve(key, manifest=manifest, known={}).key, key)

    def test_a_share_link_is_followed_to_the_post_reel_or_video_it_lands_on(self):
        watch, post = f"{SITE}/watch/?v=22", f"{SITE}/example.page/posts/7"
        cases = [
            ("p", f"{SITE}/example.page/posts/7?rdid=x",
             {post: page(owned_by_name(photo("1")), owned_by_name(photo("2"), "other", "999"))},
             [post], ["1"]),
            ("p", f"{SITE}/permalink.php?story_fbid=7&id=42",
             {f"{SITE}/permalink.php?story_fbid=7&id=42": page(photo("1", owner="42"))},
             [f"{SITE}/permalink.php?story_fbid=7&id=42"], ["1"]),
            ("r", f"{SITE}/reel/22", {watch: page(video("22"))}, [watch], ["22"]),
            ("v", f"{BASE}/videos/some-title/22/", {watch: page(video("22"), video("9"))},
             [watch], ["22"]),
        ]
        for kind, landing, pages, visited, keys in cases:
            with self.subTest(kind=kind, landing=landing):
                link = f"{SITE}/share/{kind}/1aBc/"
                saved, ctx = run(f"facebook.com/share/{kind}/1aBc",
                                 pages={link: FakePage(url=landing), **pages})
                self.assertEqual(ctx.seen.visited, [link, *visited])
                self.assertEqual([s.key for s in saved], keys)

    def test_a_share_link_that_lands_elsewhere_is_not_found_with_a_hint(self):
        link = f"{SITE}/share/p/1aBc/"
        for landed in (f"{SITE}/", f"{SITE}/groups/x/", "https://example.com/posts/1"):
            with self.subTest(landed=landed), self.assertRaises(NotFound) as caught:
                run("fbk:facebook.com/share/p/1aBc", pages={link: FakePage(url=landed)})
            self.assertTrue(caught.exception.hint)

    def test_a_page_has_no_tagged_tab_and_the_log_says_so(self):
        for typename in ("Page", "User"):
            with self.subTest(typename):
                pages = {BASE: page(header("photos_by", "photos_of", typename=typename)),
                         f"{BASE}/photos_of": page(photo("71"))}
                saved, ctx = run(categories=["tagged"], pages=pages)
                self.assertEqual([s.key for s in saved], [] if typename == "Page" else ["71"])
                self.assertEqual(("info", "tagged: this profile has no such tab") in ctx.seen.logs,
                                 typename == "Page")


class DateTest(unittest.TestCase):
    def test_there_is_no_date_hook_since_a_photo_page_embeds_no_day(self):
        # Live (2026-11-19): 9 of 9 photo pages held no timestamp, so asking cost a request each.
        self.assertIs(Facebook.date, Plugin.date)


class WalkerTest(unittest.TestCase):
    def test_videos_take_dash_else_every_progressive_url_hd_first(self):
        [item] = media_items(video("22", dash=DASH))
        self.assertEqual((item.url, item.audio),
                         ("https://cdn.example/1080.mp4", "https://cdn.example/a95.mp4"))
        [item] = media_items(video("22", dash="<MPD/>"))
        self.assertEqual((item.url, item.audio),
                         (("https://cdn.example/hd.mp4", "https://cdn.example/sd.mp4"), None))

    def test_what_is_left_out(self):
        thumb = {**video_node("22"), "preferred_thumbnail": {
            "id": "23", "image": {"uri": "https://cdn.example/t.jpg"}}}
        preview_reel = {"__typename": "Reel", "id": "24",
                        "image": {"uri": "https://cdn.example/r.jpg"},
                        "preferred_thumbnail": {
                            "id": "25", "image": {"uri": "https://cdn.example/rt.jpg"}}}
        tile = {"__typename": "TimelineAppCollectionItem", "id": "YXBw",
                "image": {"uri": "https://cdn.example/small.jpg"}, "node": photo("21")["data"]}
        cases = [
            ("a video's thumbnails", {"data": thumb}, None, ["22"]),
            ("a preview reel without video urls", {"data": preview_reel}, None, []),
            ("a grid tile, for the photo inside", {"data": {"edges": [{"node": tile}]}}, None,
             ["21"]),
            ("the header", header("photos"), None, []),
            ("another account's", {"data": [photo("1", owner="999"), photo("2", owner="42")]},
             "42", ["2"]),
            ("nothing without an owner", {"data": [photo("1", owner="999")]}, None, ["1"]),
            ("a repeat", {"data": [photo("1"), photo("1")]}, None, ["1"]),
        ]
        for label, data, owner, ids in cases:
            with self.subTest(label):
                self.assertEqual([item.id for item in media_items(data, owner=owner)], ids)

    def test_the_tabs_page_info_and_the_tiles_feeds_first_answer(self):
        comment = {"data": {"node": {"story": {"feedback": {"comments": {
            "page_info": {"has_next_page": True, "end_cursor": "x"}}}}}}}
        self.assertIsNone(next_page(comment))
        self.assertEqual(next_page(info("c9")), (True, "c9"))
        read = tab_cursor()
        # 2026-11, live: the Tiles feed's "no next page" first says nothing about the tab.
        self.assertEqual([read(info("")), read(info("c3")), read(info(""))],
                         [None, (True, "c3"), (False, None)])
        # Sidebar / chrome page_info never ends a tab or posts timeline.
        tiles_pi = {"data": {"node": {"profile_tile_sections": {
            "page_info": {"has_next_page": False, "end_cursor": "photos"}}}}}
        viewer_pi = {"data": {"viewer": {"actor": {"profiles": {
            "page_info": {"has_next_page": False, "end_cursor": None}}}}}}
        nav_pi = {"data": {"user": {"timeline_nav_app_sections": {
            "page_info": {"has_next_page": False, "end_cursor": "x"}}}}}
        self.assertIsNone(next_page(tiles_pi))
        self.assertIsNone(next_page(viewer_pi))
        self.assertIsNone(next_page(nav_pi))
        tl_more = {"data": {"page_info": {"has_next_page": True, "end_cursor": "c1"}},
                   "path": ["node", "timeline_list_feed_units"]}
        tl_end = {"data": {"page_info": {"has_next_page": False, "end_cursor": None}},
                  "path": ["node", "timeline_list_feed_units"]}
        posts_read = tab_cursor("posts")
        self.assertEqual([posts_read(tl_more), posts_read(tiles_pi), posts_read(tl_end)],
                         [(True, "c1"), None, (False, None)])

    def test_video_style_wrappers_walked_and_sidebar_shared_posts_filtered(self):
        # Video nested under Comet *Video*StyleRenderer must be extracted, not skipped.
        wrapped = {"data": {"node": {
            "__typename": "Story", "creation_time": NOON, "actors": [{"id": "42"}],
            "attachments": [{"styles": {
                "__typename": "StoryAttachmentUnifiedLightweightVideoStyleRenderer",
                "attachment": {"media": {**video_node("77"), "owner": {"id": "42"}}},
            }}],
        }}}
        self.assertEqual([it.id for it in media_items(wrapped, owner="42")], ["77"])
        # Sidebar tiles, delegate cover_photo, and shared attached_story of another actor are out.
        noisy = {"data": {"node": {
            "profile_tile_sections": {"edges": [photo("88")]},
            "delegate_page": {"cover_photo": {"photo": photo("89")["data"]["node"]}},
            "attached_story": {"actors": [{"id": "999"}], "media": photo("90")["data"]["node"]},
            "comet_sections": {"attached_story": {"media": photo("91")["data"]["node"]}},
        }}}
        self.assertEqual([it.id for it in media_items(noisy, owner="42")], [])
        # Reels tab skips stray images.
        saved, _ = run(categories=["reels"], pages={
            BASE: page(header(*TABS, user_id="42")),
            f"{BASE}/reels_tab": page(video("22"), photo("99")),
        })
        self.assertEqual([s.key for s in saved], ["22"])

    def test_highlights_paginate_and_posts_cross_reference_dates_to_photos(self):
        hl_tile = {"data": {"node": {"profile_tile_sections": {"edges": [{"node": {
            "profile_tile_views": {"nodes": [{"view_style_renderer": {
                "__typename": "ProfileTileViewFeaturedHighlightsRenderer",
                "view": {
                    "all_featured_highlights": {"count": 2},
                    "profile_tile_items": {
                        "edges": [{"node": {"node": {
                            "__typename": "StoryHighlightContainer",
                            "id": "101", "name": "Trip 2026",
                        }}}],
                        "page_info": {"has_next_page": True, "end_cursor": "c1"},
                    },
                },
            }}]},
        }}]}}}}
        hl_more = {"data": {"node": {
            "__typename": "ProfileTileView",
            "profile_tile_items": {
                "edges": [{"node": {"node": {
                    "__typename": "StoryHighlightContainer",
                    "id": "102", "name": "Work",
                }}}],
                "page_info": {"has_next_page": False, "end_cursor": None},
            },
        }}}
        hl1_bucket = {"data": {"bucket": {"unified_stories_with_notes": {"edges": [
            {"node": {"creation_time": NOON, "attachments": [{"media": video_node("201")}]}},
        ]}}}}
        hl2_bucket = {"data": {"bucket": {"unified_stories_with_notes": {"edges": [
            {"node": {"creation_time": NOON, "attachments": [{"media": {
                "__typename": "Photo", "id": "202", "image": {"uri": "https://cdn.example/h.jpg"},
            }}]}},
        ]}}}}
        base_page = FakePage(
            embedded(header(*TABS, user_id="42"), photo("501", stamp=NOON), hl_tile),
            shown=[HIGHLIGHT_NEXT],
            feeds=[Sent(GRAPHQL, hl_more, click=HIGHLIGHT_NEXT)],
        )
        pages = {
            BASE: base_page,
            f"{SITE}/stories/101/?source=profile_highlight": page(hl1_bucket),
            f"{SITE}/stories/102/?source=profile_highlight": page(hl2_bucket),
            f"{BASE}/photos_by": page(photo("501", stamp=0)),
        }
        saved, _ = run(categories=["posts", "highlights"], pages=pages)
        paths = sorted((s.path, s.key) for s in saved)
        self.assertEqual(paths, [
            ("highlights/Trip_2026/2023-11-15_201.mp4",
             "highlights/Trip_2026/2023-11-15_201.mp4"),
            ("highlights/Work/2023-11-15_202.jpg",
             "highlights/Work/2023-11-15_202.jpg"),
            ("posts/2023-11-15_501.jpg", "501"),
        ])
        # Verify dates learned in posts carry over to undated photos on the same profile.
        prof = profile_of(BASE, [header(*TABS, user_id="42"), photo("501", stamp=NOON)], [])
        ctx = FakeCtx(Facebook, pages={f"{BASE}/photos_by": page(photo("501", stamp=0))})

        async def run_both():
            fb = Facebook()
            _ = [p async for p in fb.posts(ctx, prof)]
            return [p async for p in fb.photos(ctx, prof)]

        photo_posts = asyncio.run(run_both())
        self.assertEqual([(p.id, p.date) for p in photo_posts], [("501", NOON)])


class AntiBanTest(unittest.TestCase):
    def test_pace_and_lock_walls_and_fatal_graphql_codes(self):
        self.assertGreaterEqual(Facebook.pace, 1.5)
        for wall_path in ("/checkpoint", "/checkpoint/block", "/two_step_verification",
                          "/account_disabled", "/suspended"):
            with self.subTest(wall=wall_path):
                ctx = FakeCtx(Facebook, "fbk:example.page", pages={
                    f"{SITE}/example.page": FakePage(url=f"{SITE}{wall_path}"),
                })
                with self.assertRaises(LoginError) as caught:
                    collect(Facebook, "fbk:example.page", ctx=ctx, categories=["posts"])
                self.assertFalse(caught.exception.retryable)

        for code in ("190", "459", "1346003", "1357001", "1357004"):
            with self.subTest(code=code):
                ctx = FakeCtx(Facebook, "fbk:example.page", pages={
                    f"{SITE}/example.page": FakePage(
                        html=embedded({"data": {"user": {"id": "42"}}}),
                        feeds=[Sent(GRAPHQL, {"errors": [{"code": int(code)}]}, scroll=1)],
                    ),
                })
                with self.assertRaises(LoginError) as caught:
                    collect(Facebook, "fbk:example.page", ctx=ctx, categories=["posts"])
                self.assertFalse(caught.exception.retryable)

    def test_no_progress_429_reload_aborts_instead_of_looping(self):
        from unittest import mock

        queue = [
            FakePage(
                html=embedded({"data": {"user": {"id": "42"}}}),
                feeds=[
                    Sent(GRAPHQL, {"data": [photo("1")]}),
                    Sent(GRAPHQL, status=429, scroll=1),
                ],
            ),
            FakePage(
                html=embedded({"data": {"user": {"id": "42"}}}),
                feeds=[Sent(GRAPHQL, status=429)],
            ),
        ]
        ctx = FakeCtx(Facebook, "fbk:example.page", pages={})
        orig_open = ctx.open

        async def async_open(target_url):
            if target_url == f"{SITE}/example.page":
                ctx._pages[target_url] = queue.pop(0)
            return await orig_open(target_url)

        with mock.patch.object(ctx, "open", side_effect=async_open):
            with self.assertRaises(Blocked) as caught:
                collect(Facebook, "fbk:example.page", ctx=ctx, categories=["posts"])
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(ctx.seen.holds), 1)

    def test_code_368_halts_immediately_and_rate_limit_waits_conservatively(self):
        self.assertGreaterEqual(Facebook.pace, 2.0)
        limited = {"errors": [{"code": 1675004, "message": "rate limited"}]}
        pages = {
            BASE: page(header(*TABS)),
            f"{BASE}/photos_by": page(header(*TABS), photo("1"),
                                      scroll=[[limited, photo("2")]]),
        }
        saved, ctx = run(categories=["photos"], pages=pages)
        self.assertEqual([s.key for s in saved], ["1", "2"])
        self.assertGreaterEqual(ctx.seen.holds[0][1], 300.0)

        spam = {"errors": [{"code": 368, "message": "temporarily blocked"}]}
        ctx_368 = FakeCtx(Facebook, "fbk:example.page", pages={
            BASE: page(header(*TABS)),
            f"{BASE}/photos_by": page(header(*TABS), scroll=[[spam]]),
        })
        with self.assertRaises(Blocked) as caught:
            collect(Facebook, "fbk:example.page", ctx=ctx_368, categories=["photos"])
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(ctx_368.seen.holds, [])


if __name__ == "__main__":
    unittest.main()
