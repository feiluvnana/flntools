"""Threads on plugin API 2 (`src/plugins/thr.py`), run on `FakeCtx`: no network, no browser.

Every response shape is faked from what the plugin assumes; each is "confirm live".
"""

import json
import unittest

import fakes

from flnd.engine.resolve import Manifest, resolve
from flnd.errors import NotFound, UsageError
from flnd.plugin import Target
from flnd.plugins import _manifest
from flnd.plugins.thr import Threads
from flnd.testing import FakeCtx, FakePage, Sent, check, collect
from flnkit import net as _net

SITE = "https://www.threads.com"
GRAPHQL = f"{SITE}/api/graphql"
# 2023-11-15 12:00 UTC: the same day in every time zone.
STAMP = 1700049600
DAY = "2023-11-15"
THROTTLE = {"message": "Please wait a few minutes before you try again.", "status": "fail"}


def embedded(*results):
    """A page whose HTML embeds `results` as Meta's server does."""
    blocks = "".join('<script type="application/json">'
                     f'{json.dumps({"require": [["x", {"__bbox": {"result": result}}]]})}</script>'
                     for result in results)
    return f"<html><body>{blocks}</body></html>"


def image(url, width=640):
    return {"image_versions2": {"candidates": [
        {"url": url.replace(".jpg", "_s.jpg"), "width": 150, "height": 150},
        {"url": url, "width": width, "height": width}]}}


def video(url):
    return {**image(url.replace(".mp4", ".jpg")), "video_versions": [
        {"url": url.replace(".mp4", "_low.mp4"), "width": 480, "height": 854},
        {"url": url, "width": 720, "height": 1280}]}


def thread(code, owner="example_creator", **media):
    return {"code": code, "taken_at": STAMP, "user": {"username": owner}, **media}


def tab(*posts, more=None):
    """A tab's result; `more` is the next cursor, "" for the last page, None for none."""
    connection = {"edges": [{"node": {"thread_items": [{"post": post}]}} for post in posts]}
    if more is not None:
        connection["page_info"] = {"has_next_page": bool(more), "end_cursor": more or None}
    return {"data": {"mediaData": connection}}


PROFILE = {"data": {"user": {"hd_profile_pic_versions": [
    {"url": "https://cdn.example/v/s_n.jpg", "width": 150, "height": 150},
    {"url": "https://cdn.example/v/123_n.jpg?x=1", "width": 320, "height": 320}]}}}
POSTS = tab(thread("P1", **image("https://cdn.example/p1.jpg")),
            thread("CAR", carousel_media=[image("https://cdn.example/c0.jpg"),
                                          video("https://cdn.example/c1.mp4")]),
            thread("TXT"), thread("OTHER", owner="example_user",
                                  **image("https://cdn.example/o.jpg")), more="c1")
MORE = tab(thread("V1", **video("https://cdn.example/v1.mp4")), more="")
REPLIES = tab(thread("PARENT", owner="example_user", **image("https://cdn.example/pa.jpg")),
              thread("RE1", text_post_app_info={"reply_to_author": {"username": "example_user"}},
                     **image("https://cdn.example/r1.jpg")), more="")
REPOSTS = tab(thread("RP1", owner="example_user", **image("https://cdn.example/rp1.jpg")),
              more="")
PAGES = {
    f"{SITE}/@example_creator": FakePage(embedded(PROFILE, POSTS), feeds=[
        Sent(GRAPHQL, MORE, scroll=1)]),
    f"{SITE}/@example_creator/replies": FakePage(embedded(REPLIES)),
    f"{SITE}/@example_creator/reposts": FakePage(embedded(REPOSTS)),
}
SAVED = [
    ("posts/2023-11-15_CAR_0.jpg", "CAR_0"),
    ("posts/2023-11-15_CAR_1.mp4", "CAR_1"),
    ("posts/2023-11-15_P1.jpg", "P1_0"),
    ("posts/2023-11-15_V1.mp4", "V1_0"),
    ("profile/123_n.jpg", "profile/123_n.jpg"),
    ("replies/2023-11-15_RE1.jpg", "RE1_0"),
]


def fake(pages=None, **options):
    return FakeCtx(Threads, routes={}, pages=PAGES if pages is None else pages, **options)


def run(target="thr:example_creator", categories=None, ctx=None):
    return collect(Threads, target, ctx=ctx or fake(), categories=categories)


class RoutesTest(unittest.TestCase):
    def test_accepted_and_refused_forms(self):
        accepted = [
            ("thr:example_user", "user", "example_user"),
            ("thr:@example_user", "user", "example_user"),
            ("threads.com/@example_user", "user", "example_user"),
            ("https://www.threads.com/@example_user?hl=en", "user", "example_user"),
            ("https://www.threads.net/@example_user", "user", "example_user"),
            ("thr:a.b_c9", "user", "a.b_c9"),
            ("thr:post/C1abc-DEF_g", "post", "post/C1abc-DEF_g"),
            ("https://www.threads.com/@example_user/post/C1abc", "post", "post/C1abc"),
            ("threads.net/@example_user/post/C1abc", "post", "post/C1abc"),
        ]
        for text, kind, value in accepted:
            with self.subTest(text=text):
                self.assertEqual(Threads.resolve(text), Target(f"thr:{value}", kind=kind))
        name = "a user name is 1 to 30 letters, digits, . or _"
        tabs = '"thr:example_user|replies"'
        refused = [
            (f"thr:{'a' * 31}", name), ("thr:café", name),
            ("threads.com/@example_user/replies", tabs),
            ("https://www.threads.net/@example_user/reposts", tabs),
            ("threads.com/search?q=cats", "thr has no search"),
            ("threads.com/activity", "that's not a profile"),
            ("threads.com/settings", "that's not a profile"),
        ]
        for text, message in refused:
            with self.subTest(text=text), self.assertRaisesRegex(UsageError, message):
                Threads.resolve(text)
        self.assertIsNone(Threads.resolve("https://www.threads.com/"))

    def test_a_query_and_another_kind_are_refused(self):
        manifest = Manifest(_manifest.PLUGINS)
        for text, message in [("thr?cats", "thr has no search"),
                              ("threads.com/search?q=cats", "thr has no search"),
                              ("thr:thing/C1abc", "thing isn't a post kind of thr")]:
            with self.subTest(text=text), self.assertRaisesRegex(UsageError, message):
                resolve(text, manifest=manifest, known={})

    def test_a_key_round_trips_to_the_same_target(self):
        for link in ("https://www.threads.com/@example_user/post/C1abc",
                     "https://www.threads.net/@example_user"):
            with self.subTest(link=link):
                found = Threads.resolve(link)
                self.assertEqual(Threads.resolve(found.text), found)

    def test_declarations(self):
        self.assertEqual((Threads.api, Threads.pace, Threads.domains),
                         (2, 2.0, ("threads.com", "threads.net")))
        self.assertEqual(Threads.login.names, ("sessionid",))
        route = next(known for known in Threads.routes if known.kind == "user")
        self.assertEqual([(c.name, c.own) for c in route.categories],
                         [("posts", True), ("replies", True), ("reposts", False),
                          ("profile", True)])
        self.assertEqual([known.kind for known in Threads.routes if known.single], ["post"])
        self.assertEqual(len(check(Threads)), 3)


class ProfileTest(unittest.TestCase):
    def test_every_category_is_named_and_keyed(self):
        ctx = fake()
        saved = run(categories=["all"], ctx=ctx)
        self.assertEqual(sorted((item.path, item.key) for item in saved),
                         sorted(SAVED + [("reposts/2023-11-15_RP1.jpg", "RP1_0")]))
        # The embedded page is read first, the scroll brings the rest; a carousel's best files.
        by_path = {item.path: item for item in saved}
        self.assertEqual(by_path["posts/2023-11-15_CAR_1.mp4"].url, "https://cdn.example/c1.mp4")
        self.assertEqual(by_path["profile/123_n.jpg"].url, "https://cdn.example/v/123_n.jpg?x=1")
        self.assertEqual(ctx.seen.visited, [f"{SITE}/@example_creator",
                                            f"{SITE}/@example_creator/replies",
                                            f"{SITE}/@example_creator/reposts"])

    def test_the_default_takes_only_the_accounts_own(self):
        folders = {item.path.split("/")[0] for item in run()}
        self.assertEqual(folders, {"posts", "replies", "profile"})

    def test_a_replies_tab_keeps_only_the_profiles_own_posts(self):
        self.assertEqual([item.path for item in run(categories=["replies"])],
                         [f"replies/{DAY}_RE1.jpg"])

    def test_a_please_wait_answer_holds_and_the_feed_goes_on(self):
        pages = {**PAGES, f"{SITE}/@example_creator": FakePage(embedded(POSTS), feeds=[
            Sent(GRAPHQL, THROTTLE, scroll=1), Sent(GRAPHQL, MORE, scroll=2)])}
        ctx = fake(pages=pages)
        saved = run(categories=["posts"], ctx=ctx)
        self.assertIn(f"posts/{DAY}_V1.mp4", [item.path for item in saved])
        self.assertEqual([reason for reason, _, _ in ctx.seen.holds],
                         ["Threads asked to wait a few minutes"])


class PostTest(unittest.TestCase):
    def single(self, **extra):
        media = thread("SINGLE", **image("https://cdn.example/a.jpg"), **extra)
        page = FakePage(embedded({"data": {"data": {"edges": [
            {"node": {"thread_items": [{"post": media}]}}]}}}))
        return fake(pages={f"{SITE}/t/SINGLE": page})

    def test_a_post_is_filed_in_its_owners_folder(self):
        for link, extra, folder in [
            ("https://www.threads.com/@example_creator/post/SINGLE", {},
             "example_creator/posts"),
            ("thr:post/SINGLE", {"text_post_app_info": {"reply_to_author": {"id": "1"}}},
             "example_creator/replies"),
        ]:
            with self.subTest(link=link):
                [found] = run(link, ctx=self.single(**extra))
                self.assertEqual(fakes.filed(Threads, link, found), folder)
                self.assertEqual(found.key, "SINGLE_0")

    def test_a_page_without_the_post_is_not_found(self):
        ctx = fake(pages={f"{SITE}/t/NONE": FakePage(embedded({"data": {}}))})
        with self.assertRaisesRegex(NotFound, "its page holds no post"):
            run("thr:post/NONE", ctx=ctx)

    def test_a_post_without_files_is_not_found(self):
        page = FakePage(embedded(tab(thread("TXT"))))
        ctx = fake(pages={f"{SITE}/t/TXT": page})
        with self.assertRaisesRegex(NotFound, "no files"):
            run("thr:post/TXT", ctx=ctx)

    def test_blocked_reads_only_the_please_wait_answer(self):
        plugin = Threads()
        for body, held in [(THROTTLE, 0.0), ({"data": {}}, None)]:
            with self.subTest(body=body):
                reply = fakes_response(body)
                self.assertEqual(plugin.blocked(reply), held)
        self.assertIsNone(plugin.blocked(fakes_response("<html></html>")))


def fakes_response(body):
    data = json.dumps(body) if isinstance(body, dict) else body
    return _net.Response(200, f"{SITE}/api/graphql", {}, data.encode())


if __name__ == "__main__":
    unittest.main()
