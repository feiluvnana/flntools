"""Twitter / X on plugin API 2 (src/plugins/twt.py), on `FakeCtx`: every target form, each
category's names and keys, one tweet, a search reused by `post`, stale query ids, `sign`, the
buckets, and PLAN.md's S2, S4 and S21. The engine's early stops are the engine's own tests.
"""

import asyncio
import json
import unittest
from datetime import date

import no_profiles  # noqa: F401

from flnd.errors import LoginError, NotFound, SiteError
from flnd.plugin import Range, Request, Target
from flnd.plugins.twt import (
    QUERIES,
    TIMELINES,
    TWITTER_BEARER,
    Twitter,
    read_timeline_page,
    tweet_posts,
)
from flnd.testing import FakeCtx, FakePage, Reply, Sent, collect
from flnkit.clock import FAKE_WALL

API = "https://x.com/i/api/graphql/"
USER_ID, OTHER_ID, DAY = "42", "77", "2026-11-09"
HOME = "https://x.com/"
SCRIPT = "https://abs.twimg.com/responsive-web/client-web/main.abc123de.js"


def url(query):
    return f"{API}{QUERIES[query]}/{query}"


def photo(name, extension="jpg"):
    return {"type": "photo", "media_url_https": f"https://pbs.example/media/{name}.{extension}"}


def video(name):
    return {"type": "video", "video_info": {"variants": [
        {"content_type": "application/x-mpegURL", "url": f"https://video.example/{name}.m3u8"},
        {"content_type": "video/mp4", "bitrate": 1, "url": f"https://video.example/{name}_lo.mp4"},
        {"content_type": "video/mp4", "bitrate": 9, "url": f"https://video.example/{name}.mp4"}]}}


def tweet(tweet_id, media, *, author=USER_ID, reply_to=None, retweet=None, by=None):
    legacy = {"id_str": tweet_id, "user_id_str": author, "extended_entities": {"media": media},
              "created_at": "Mon Nov 09 10:00:00 +0000 2026"}
    if reply_to:
        legacy["in_reply_to_status_id_str"] = reply_to
    if retweet:
        legacy["retweeted_status_result"] = {"result": retweet}
    found = {"__typename": "Tweet", "rest_id": tweet_id, "legacy": legacy}
    if by:
        found["core"] = {"user_results": {"result": {"core": {"screen_name": by}}}}
    return found


def item(result):
    return {"itemContent": {"tweet_results": {"result": result}}}


def page(*entries, cursor="next", search=False):
    """A timeline page (or a search's): its entries, then its bottom cursor."""
    added = [{"entryId": f"e{n}", "content": entry} for n, entry in enumerate(entries)]
    added.append({"entryId": "cursor-bottom", "content": {"cursorType": "Bottom", "value": cursor}})
    timeline = {"timeline": {"instructions": [{"type": "TimelineAddEntries", "entries": added}]}}
    if search:
        return {"data": {"search_by_raw_query": {"search_timeline": timeline}}}
    return {"data": {"user": {"result": {"timeline": timeline}}}}


def sizes(link):
    return tuple(f"{link}?name={size}" for size in ("orig", "4096x4096", "large", "medium"))


def tweet_result(result):
    return Reply({"data": {"tweetResult": {"result": result}}})


USER = {"data": {"user": {"result": {
    "rest_id": USER_ID,
    "avatar": {"image_url": "https://pbs.example/profile_images/9/me_normal.JPG"},
    "banner": {"image_url": "https://pbs.example/profile_banners/42/1683721221"}}}}}


def sent(link, answers):
    """A tab's answers to `link`: the first as the page loads, the n-th on its n-th scroll."""
    return [Sent(link, data, scroll=n or None) for n, data in enumerate(answers)]


def tabs(profile="https://x.com/example_handle", **timelines):
    """Every timeline tab of `profile`, each showing its operation's pages: `{category: pages}`."""
    return {profile + timeline.tab: FakePage(
        feeds=sent(f"{API}id/{timeline.query}", timelines.get(category, ())))
        for category, timeline in TIMELINES.items()}


def fake(target="twt:example_handle", *, user=USER, routes=None, plugin=Twitter, **given):
    """A `FakeCtx` whose user lookups answer `user` (a body, or a `Reply`), and where x.com's
    page, read to refresh stale query ids, answers 404 unless `routes` gives it."""
    answer = user if isinstance(user, Reply) else Reply(user)
    return FakeCtx(plugin, target, routes={
        url("UserByScreenName"): answer, url("UserByRestId"): answer,
        HOME: Reply(status=404), **(routes or {})}, **given)


def listed(plugin, ctx, route, value):
    """What a route with no categories yields, run on the fake clock."""
    async def run():
        return [found async for found in getattr(plugin, route)(ctx, value)]
    return asyncio.run(run())


def asked(ctx):
    """(operation or URL, bucket) of each request."""
    return [(link.rsplit("/", 1)[-1] if link.startswith(API) else link, options["bucket"])
            for _, link, options in ctx.seen.requests]


NOT_A_PROFILE = "that's not a profile"
# Every form twt reads, and what it becomes: (kind, value), None for no route, or a refusal.
# The kinds and values are the keys of the store and the history: a change here is a migration.
ROUTES = [
    ("twt:example_handle", ("user", "example_handle")),
    ("twt?example words", ("search", "example words")),
    ("twt:@example_handle", ("user", "example_handle")),
    ("twt:example.handle", ("user", "example.handle")),
    ("twt:x.com", ("user", "x.com")),
    ("twt:1000000001", ("user", "1000000001")),
    ("https://x.com/i/user/1000000001", ("user", "1000000001")),
    ("https://twitter.com/i/user/1000000001", ("user", "1000000001")),
    ("https://x.com/example_handle", ("user", "example_handle")),
    ("twitter.com/example_handle", ("user", "example_handle")),
    ("https://twitter.com/example_handle/media", ("user", "example_handle")),
    ("https://www.x.com/example_handle/", ("user", "example_handle")),
    ("https://mobile.twitter.com/example_handle", ("user", "example_handle")),
    ("http://x.com/example_handle#top", ("user", "example_handle")),
    ("https://x.com/example_handle?lang=en", ("user", "example_handle")),
    ("https://x.com/@example_handle", ("user", "example_handle")),
    ("https://x.com/example_handle/status/abc", ("user", "example_handle")),
    ("https://x.com/example_handle/status/1790000000000000000/", ("post", "1790000000000000000")),
    ("https://x.com/example_handle/status/1790?s=20", ("post", "1790")),
    ("https://twitter.com/example_handle/status/1790/photo/1", ("post", "1790")),
    ("https://www.twitter.com/example_handle/status/1790", ("post", "1790")),
    ("twt:x.com/example_handle/status/1790", ("post", "1790")),
    ("twt:twitter.com/example_handle/status/1790", ("post", "1790")),
    ("https://x.com/status/1790", ("post", "1790")),
    ("https://twitter.com/status/1790", ("post", "1790")),
    ("https://x.com/i/web/status/1790", ("post", "1790")),
    ("https://twitter.com/i/web/status/1790", ("post", "1790")),
    *[(f"https://{host}/{path}", NOT_A_PROFILE) for host in ("x.com", "twitter.com") for path in (
        "home", "explore", "search?q=example", "settings", "notifications", "messages/1",
        "hashtag/tag", "i/flow/login", "intent/tweet", "compose/post", "login")],
    ("https://x.com/i", NOT_A_PROFILE),
    ("twt:https://x.com/explore", NOT_A_PROFILE),
    *[(text, None) for text in (
        "twt:not a name", "twt:ex-ample", "twt:", "twt:example.com/example_handle",
        "twt:https://example.com/example_handle", "https://notx.com/example_handle",
        "https://x.com/", "https://x.com/example handle")],
]


class RoutesTest(unittest.TestCase):
    def test_every_form(self):
        for text, expected in ROUTES:
            with self.subTest(text=text):
                if expected == NOT_A_PROFILE:
                    with self.assertRaisesRegex(Exception, f"^twt: {expected}"):
                        Twitter.resolve(text)
                    continue
                found = Twitter.resolve(text)
                if expected is None:
                    self.assertIsNone(found)
                else:
                    sep = "?" if expected[0] == "search" else ":"
                    self.assertEqual(found, Target(f"twt{sep}{expected[1]}", kind=expected[0]))

    def test_each_category_declares_its_order_selection_and_tab(self):
        user = next(known for known in Twitter.routes if known.kind == "user")
        profile = {"page": "https://x.com/example_handle"}
        self.assertEqual(
            [(found.name, found.newest_first, found.own, found.link(profile))
             for found in user.categories],
            [("posts", True, True, "https://x.com/example_handle"),
             ("media", True, True, "https://x.com/example_handle/media"),
             ("highlights", False, True, "https://x.com/example_handle/highlights"),
             ("reposts", False, False, "https://x.com/example_handle/reposts"),
             ("replies", True, True, "https://x.com/example_handle/with_replies"),
             ("profile", False, True, "")])


RETWEETED = tweet("500", [photo("rt")], author=OTHER_ID)
NO_DATE = tweet("700", [photo("a"), {"type": "animated_gif"}, video("c")])
del NO_DATE["legacy"]["created_at"]
SKIPS = tweet("800", [{"type": "animated_gif"}, photo("s"),
                      {"type": "video", "video_info": {"variants": []}}, video("t")])
ODD = tweet("900", [{"type": "photo", "media_url_https": "https://pbs.example/media/plain"},
                    {"type": "photo", "media_url_https": "https://pbs.example/media/x.jpeg"}])
EVERYTHING = dict(
    posts=[page(item(tweet("100", [photo("p", "JPG"), video("v")])),
                item(tweet("101", [photo("r")], retweet=RETWEETED)),
                item(NO_DATE), item(SKIPS), item(ODD))],
    media=[page({"items": [{"item": item(tweet("100", [photo("p", "JPG")]))},
                           {"item": item(tweet("200", [video("m")]))}, {"item": item(SKIPS)}]})],
    highlights=[page(item(tweet("300", [photo("h")])))],
    reposts=[page(item(tweet("400", [], retweet=RETWEETED)), item(tweet("401", [photo("own")])))],
    replies=[page({"items": [{"item": item(tweet("600", [photo("parent")], author=OTHER_ID))},
                             {"item": item(tweet("601", [photo("reply")], reply_to="600"))}]})],
)
# What API 1's twt saved for those answers (tests/test_twt.py's TODAY), as (path, key).
TODAY = [
    ("posts/2026-11-09_100_0.JPG", "100_0"), ("posts/2026-11-09_100_1.mp4", "100_1"),
    ("posts/700_0.jpg", "700_0"), ("posts/700_2.mp4", "700_2"),
    ("posts/2026-11-09_800_1.jpg", "800_1"), ("posts/2026-11-09_800_3.mp4", "800_3"),
    ("posts/2026-11-09_900_0.jpg", "900_0"), ("posts/2026-11-09_900_1.jpeg", "900_1"),
    ("videos/2026-11-09_200_0.mp4", "200_0"),
    ("highlights/2026-11-09_300_0.jpg", "300_0"),
    ("reposts/2026-11-09_500_0.jpg", "500_0"),
    ("replies/2026-11-09_601_0.jpg", "601_0"),
    ("profile/avatar_9.JPG", "profile/avatar_9.JPG"),
    ("profile/banner_1683721221.jpg", "profile/banner_1683721221.jpg"),
]


class NamesAndKeysTest(unittest.TestCase):
    def test_every_category_names_and_keys_its_files_as_today(self):
        saved = collect(Twitter, "twt:example_handle", ctx=fake(pages=tabs(**EVERYTHING)),
                        categories=["all"])
        self.assertEqual([(found.path, found.key) for found in saved], TODAY)
        self.assertEqual([found.url for found in saved[:3]], [
            sizes("https://pbs.example/media/p.JPG"), "https://video.example/v.mp4",
            sizes("https://pbs.example/media/a.jpg")])
        self.assertEqual([found.url for found in saved[-2:]], [
            "https://pbs.example/profile_images/9/me.JPG",
            "https://pbs.example/profile_banners/42/1683721221/1500x500"])

    def test_a_tweet_link_is_saved_in_its_targets_own_folder(self):
        wrapped = {"__typename": "TweetWithVisibilityResults",
                   "tweet": tweet("1790", [photo("a"), video("b")])}
        ctx = fake("https://x.com/example_handle/status/1790",
                   routes={url("TweetResultByRestId"): tweet_result(wrapped)})
        saved = collect(Twitter, "https://x.com/example_handle/status/1790", ctx=ctx)
        self.assertEqual([(found.path, found.key) for found in saved],
                         [(f"{DAY}_1790_0.jpg", "1790_0"), (f"{DAY}_1790_1.mp4", "1790_1")])
        variables = json.loads(ctx.seen.requests[0][2]["params"]["variables"])
        self.assertEqual(variables["tweetId"], "1790")

    def test_a_quoted_tweets_media_and_the_card_image_come_with_the_post(self):
        quoting = tweet("1", [photo("own")])
        quoting["quoted_status_result"] = {"result": tweet("2", [photo("q"), video("clip")])}
        quoting["card"] = {"legacy": {"binding_values": [
            {"key": "thumbnail_image_original",
             "value": {"image_value": {"url": "https://pbs.example/card_img/t"}}},
            {"key": "photo_image_full_size_original",
             "value": {"image_value": {"url": "https://pbs.example/card_img/full?name=orig"}}}]}}
        posts = list(tweet_posts(quoting, "posts", None))
        self.assertEqual([post.fields["n"] for post in posts], [0, "q0", "q1", "card"])
        self.assertEqual([post.files[0].url or post.files[0].mirrors for post in posts[1:]], [
            sizes("https://pbs.example/media/q.jpg"), "https://video.example/clip.mp4",
            "https://pbs.example/card_img/full?name=orig"])


class TweetTest(unittest.TestCase):
    """S2: a deleted tweet is gone, and a 429 holds the operation's bucket and asks again."""

    LINK = "https://x.com/example_handle/status/1790"

    def test_a_tweet_the_api_doesnt_give_is_not_found(self):
        cases = [("deleted", Reply(status=404)), ("empty", Reply({"data": {"tweetResult": {}}}))]
        for name, answer in cases:
            with self.subTest(name):
                ctx = fake(self.LINK, routes={url("TweetResultByRestId"): answer})
                with self.assertRaises(NotFound):
                    collect(Twitter, self.LINK, ctx=ctx)

    def test_a_429_holds_the_operations_bucket_and_asks_again(self):
        # X sends `x-rate-limit-reset`, the Unix time its limit ends; others `Retry-After`.
        cases = [("x-rate-limit-reset", {"x-rate-limit-reset": str(int(FAKE_WALL) + 60)}),
                 ("retry-after", {"retry-after": "60"})]
        for name, headers in cases:
            with self.subTest(name):
                answers = [Reply(status=429, headers=headers),
                           tweet_result(tweet("1790", [photo("a")]))]
                ctx = fake(self.LINK,
                           routes={url("TweetResultByRestId"): lambda _, a=answers: a.pop(0)})
                saved = collect(Twitter, self.LINK, ctx=ctx)
                self.assertEqual([found.key for found in saved], ["1790_0"])
                self.assertEqual([("HTTP 429" in reason, bucket)
                                  for reason, _, bucket in ctx.seen.holds],
                                 [(True, "TweetResultByRestId")])
                self.assertGreaterEqual(ctx.seen.holds[0][1], 60.0)


class SearchTest(unittest.TestCase):
    SEARCH = "https://x.com/search?q=example+words&src=typed_query&f=live"
    RESPONSE = f"{API}abc/SearchTimeline"

    def test_a_search_yields_its_tweets_with_media_and_post_reads_them_with_no_request(self):
        plugin = Twitter()
        ctx = FakeCtx(plugin, "twt?example words", pages={self.SEARCH: FakePage(feeds=sent(
            self.RESPONSE, [page(item(tweet("1", [photo("a")])),
                                 item(tweet("2", [{"type": "animated_gif"}])),
                                 item(tweet("3", [video("v")])), cursor="c1", search=True),
                            page(item(tweet("1", [photo("a")])), item(tweet("4", [photo("b")])),
                                 cursor="c2", search=True),
                            # X answers the last page with its cursors alone.
                            page(cursor="c3", search=True)]))})
        found = listed(plugin, ctx, "search", "example words")
        self.assertEqual([str(target) for target in found], [
            "https://x.com/i/web/status/1", "https://x.com/i/web/status/3",
            "https://x.com/i/web/status/4"])
        self.assertEqual(ctx.seen.visited, [self.SEARCH])
        self.assertEqual(Twitter.resolve(str(found[0])), Target("twt:1", kind="post"))
        # The discovered tweet runs on the same instance: its files, and no API call.
        later = FakeCtx(plugin, str(found[0]))
        saved = collect(Twitter, str(found[0]), ctx=later)
        self.assertEqual([found.path for found in saved], [f"{DAY}_1_0.jpg"])
        self.assertEqual(later.seen.requests, [])

    def test_when_is_asked_of_x_as_search_operators(self):
        asked_for = ("https://x.com/search?q=example+since%3A2026-11-01+until%3A2026-11-10"
                     "&src=typed_query&f=live")
        plugin = Twitter()
        ctx = FakeCtx(plugin, "twt?example", pages={asked_for: FakePage()},
                      when=Range(date(2026, 11, 1), date(2026, 11, 9)))
        self.assertEqual(listed(plugin, ctx, "search", "example"), [])
        # X's `until:` is exclusive, so it is the day after the last.
        self.assertEqual(ctx.seen.visited, [asked_for])


class TimelineTest(unittest.TestCase):
    def test_grid_items_and_the_pinned_tweet_are_read_the_pinned_one_unordered(self):
        older = tweet("2", [photo("b")])
        older["legacy"]["created_at"] = "Mon Nov 02 10:00:00 +0000 2026"
        first = page(item(tweet("3", [photo("c")])))
        # X sends the pinned tweet before the newest ones.
        first["data"]["user"]["result"]["timeline"]["timeline"]["instructions"][:0] = [
            {"type": "TimelinePinEntry", "entry": {"entryId": "p", "content": item(older)}},
            {"type": "TimelineAddToModule",
             "moduleItems": [{"entryId": "m", "item": item(tweet("1", [photo("a")]))}]}]
        tweets, cursor, pinned = read_timeline_page(first)
        self.assertEqual(([found["rest_id"] for found in tweets], cursor, pinned),
                         (["2", "1", "3"], "next", {"2"}))
        # An older pinned tweet before newer ones neither breaks the order nor stops a sync.
        saved = collect(Twitter, "twt:example_handle", ctx=fake(pages=tabs(posts=[first])),
                        categories=["posts"])
        self.assertEqual([(found.post.id, found.post.pinned) for found in saved],
                         [("2", True), ("1", False), ("3", False)])

    def test_a_number_is_a_user_id_and_its_tab_is_opened_by_it(self):
        # A page of seen tweets ends the scroll, and an error body or a bad date doesn't.
        undated = tweet("2", [photo("b")])
        undated["legacy"]["created_at"] = "yesterday"
        posts = [page(item(tweet("1", [photo("a")])), cursor="c1"),
                 {"errors": [{"message": "Rate limit exceeded", "code": 88}]},
                 page(item(tweet("1", [photo("a")])), item(undated), cursor="c2"),
                 page(item(tweet("1", [photo("a")])), cursor="c3"),
                 page(item(tweet("3", [photo("c")])), cursor="c4")]
        ctx = fake("twt:42", pages=tabs("https://x.com/i/user/42", posts=posts))
        saved = collect(Twitter, "twt:42", ctx=ctx, categories=["posts"])
        self.assertEqual([found.post.id for found in saved], ["1", "2"])
        self.assertEqual(ctx.seen.visited, ["https://x.com/i/user/42"])
        self.assertEqual(json.loads(ctx.seen.requests[0][2]["params"]["variables"]),
                         {"userId": "42"})

    def test_a_tombstone_is_never_a_new_tweet(self):
        # 2026-11-16: a removed tweet has no id. A page of seen tweets and a tombstone is still
        # a page with nothing new, which ends the scroll.
        tombstone = {"__typename": "TweetTombstone", "tombstone": {"text": {"text": "gone"}}}
        posts = [page(item(tweet("1", [photo("a")])), cursor="c1"),
                 page(item(tweet("1", [photo("a")])), item(tombstone), cursor="c2"),
                 page(item(tweet("3", [photo("c")])), cursor="c3")]
        saved = collect(Twitter, "twt:example_handle", ctx=fake(pages=tabs(posts=posts)),
                        categories=["posts"])
        self.assertEqual([found.post.id for found in saved], ["1"])

    def test_the_own_timelines_are_read_in_their_order_after_one_lookup(self):
        ctx = fake(pages=tabs())
        collect(Twitter, "twt:example_handle", ctx=ctx)
        self.assertEqual(asked(ctx), [("UserByScreenName", "UserByScreenName")])
        self.assertEqual(ctx.seen.visited, [
            "https://x.com/example_handle", "https://x.com/example_handle/media",
            "https://x.com/example_handle/highlights",
            "https://x.com/example_handle/with_replies"])

    def test_replies_keep_the_users_own_by_id_or_else_by_the_authors_name(self):
        own = tweet("601", [photo("reply")], reply_to="600", author="?", by="Example_Handle")
        answered = tweet("600", [photo("parent")], author=OTHER_ID, reply_to="1",
                         by="example_creator")
        plain = tweet("603", [photo("plain")], author="?", by="example_handle")
        older = tweet("602", [photo("old")], author="?", reply_to="600")
        # Before 2025 the author's name was under `legacy`.
        older["core"] = {"user_results": {"result": {"legacy": {"screen_name": "example_handle"}}}}
        by_id = tweet("604", [photo("id")], reply_to="600")
        timeline = [page(item(answered), item(own), item(older), item(plain), item(by_id))]
        cases = [("known", "twt:example_handle", USER, ["604"]),
                 ("lookup refused", "twt:example_handle", Reply(status=500), ["601", "602"]),
                 ("unknown number", "twt:42", Reply(status=500), ["604"])]
        for name, target, user, expected in cases:
            with self.subTest(name):
                profile = ("https://x.com/i/user/42" if target == "twt:42"
                           else "https://x.com/example_handle")
                ctx = fake(target, user=user, pages=tabs(profile, replies=timeline))
                saved = collect(Twitter, target, ctx=ctx, categories=["replies"])
                self.assertEqual([found.post.id for found in saved], expected)


class LoadTest(unittest.TestCase):
    """S4: a tab that doesn't load fails its category, which is never recorded complete."""

    def test_a_tab_that_fails_to_load_raises(self):
        # A server error may pass, so it is retryable; a missing page isn't; a wall's is the kit's.
        wall = "https://x.com/i/flow/login?redirect_after_login=%2Fexample_handle"
        cases = [("missing", FakePage(status=404), NotFound, False),
                 ("server error", FakePage(status=503), SiteError, True),
                 ("login wall", FakePage(url=wall), LoginError, None)]
        for name, shown, error, retryable in cases:
            with self.subTest(name):
                with self.assertRaises(error) as raised:
                    collect(Twitter, "twt:example_handle", categories=["posts"],
                            ctx=fake(pages={"https://x.com/example_handle": shown}))
                if retryable is not None:
                    self.assertEqual(raised.exception.retryable, retryable)


class ProfileTest(unittest.TestCase):
    """S21: a lookup that fails fails `profile`, and the timelines still run."""

    def test_the_profile_fails_with_the_lookup_and_the_timelines_still_run(self):
        timeline = [page(item(tweet("1", [photo("a")])))]
        cases = [("refused", Reply(status=500), SiteError),
                 ("no user", Reply({"data": {}}), NotFound)]
        for name, user, error in cases:
            with self.subTest(name):
                ctx = fake(user=user, pages=tabs(posts=timeline))
                saved = collect(Twitter, "twt:example_handle", ctx=ctx, categories=["posts"])
                self.assertEqual([found.key for found in saved], ["1_0"])
                with self.assertRaises(error):
                    collect(Twitter, "twt:example_handle", ctx=fake(user=user),
                            categories=["profile"])


# x.com's page: its main script, and the feature switches' values (the user's config wins).
HOME_PAGE = (
    f'<html><script src="{SCRIPT}"></script><script>window.__INITIAL_STATE__={{"featureSwitch":'
    '{"defaultConfig":{"rweb_video_screen_enabled":{"value":false},'
    '"view_counts_everywhere_api_enabled":{"value":true},'
    '"longform_notetweets_inline_media_enabled":{"value":false}},"user":{"config":'
    '{"longform_notetweets_inline_media_enabled":{"value":true}}}}};</script></html>')
FLAGS = ('["rweb_video_screen_enabled","view_counts_everywhere_api_enabled",'
         '"longform_notetweets_inline_media_enabled","not_in_the_page"]')
# The web app's main script, as its bundler writes each operation.
MAIN_JS = (
    'e.exports={queryId:"NEWmedia42",operationName:"UserMedia",operationType:"query",'
    f'metadata:{{featureSwitches:{FLAGS},fieldToggles:["withArticlePlainText"]}}}};'
    'e.exports={queryId:"NEWuser42",operationName:"UserByScreenName",operationType:"query",'
    f'metadata:{{featureSwitches:{FLAGS},fieldToggles:[]}}}};'
    'e.exports={queryId:"other",operationName:"SomethingElse",operationType:"mutation",'
    'metadata:{featureSwitches:[],fieldToggles:[]}}')
FRESH = f"{API}NEWuser42/UserByScreenName"


class RefreshTest(unittest.TestCase):
    """A stale query id is refreshed from x.com's own script, once a run."""

    def stale(self, *, script=MAIN_JS, state=None):
        return fake(user=Reply(status=400), state=state, routes={
            HOME: Reply(HOME_PAGE), SCRIPT: Reply(script), FRESH: Reply(USER)})

    def test_a_stale_id_is_refreshed_and_sent_again_in_its_bucket_with_the_apps_flags(self):
        ctx = self.stale()
        saved = collect(Twitter, "twt:example_handle", ctx=ctx, categories=["profile"])
        self.assertEqual([found.key for found in saved],
                         ["profile/avatar_9.JPG", "profile/banner_1683721221.jpg"])
        self.assertEqual(asked(ctx), [("UserByScreenName", "UserByScreenName"), (HOME, None),
                                      (SCRIPT, None), ("UserByScreenName", "UserByScreenName")])
        self.assertEqual(ctx.seen.requests[3][1], FRESH)
        self.assertEqual(json.loads(ctx.seen.requests[3][2]["params"]["features"]), {
            "rweb_video_screen_enabled": False, "view_counts_everywhere_api_enabled": True,
            "longform_notetweets_inline_media_enabled": True, "not_in_the_page": False})
        self.assertIn(("info", "refreshed 2 query ids from x.com's script"), ctx.seen.logs)
        # The table is kept for the next run, which asks the fresh id first.
        later = fake(state=ctx.state, routes={FRESH: Reply(USER)}, user=Reply(status=400))
        collect(Twitter, "twt:example_handle", ctx=later, categories=["profile"])
        self.assertEqual([link for _, link, _ in later.seen.requests], [FRESH])

    def test_a_refresh_is_tried_once_a_run(self):
        plugin = Twitter()
        link = "https://x.com/example_handle/status/1"
        for target in (link, "twt:example_handle"):
            ctx = fake(target, plugin=plugin, user=Reply(status=404),
                       routes={url("TweetResultByRestId"): Reply(status=404)})
            with self.assertRaises(NotFound):
                collect(Twitter, target, ctx=ctx, categories=None if target == link
                        else ["profile"])
            self.assertEqual([found for found, _ in asked(ctx)].count(HOME),
                             int(target == link))

    def test_a_script_without_the_queries_is_a_warning(self):
        ctx = self.stale(script="nothing here")
        with self.assertRaises(SiteError):
            collect(Twitter, "twt:example_handle", ctx=ctx, categories=["profile"])
        self.assertIn(("warn", "the API's query ids look stale, and x.com's script lists none "
                               "of them"), ctx.seen.logs)

    def test_a_refusal_that_isnt_stale_is_not_refreshed(self):
        ctx = fake(user=Reply(status=500), pages=tabs())
        collect(Twitter, "twt:example_handle", ctx=ctx, categories=["posts"])
        self.assertNotIn(HOME, [found for found, _ in asked(ctx)])


class SignTest(unittest.TestCase):
    def test_the_api_alone_gets_its_headers_from_the_live_cookies(self):
        signed = Twitter().sign(Request(f"{API}q/UserMedia"), {"auth_token": "a", "ct0": "c"})
        self.assertEqual((signed.headers["authorization"], signed.headers["x-csrf-token"],
                          signed.headers["x-twitter-auth-type"]),
                         (TWITTER_BEARER, "c", "OAuth2Session"))
        self.assertNotIn("x-twitter-auth-type",
                         Twitter().sign(Request(f"{API}q/UserMedia"), {"ct0": "d"}).headers)
        # 2026-11: x.com's page answered the bearer token with 401, so it gets none.
        self.assertIsNone(Twitter().sign(Request(HOME), {"auth_token": "a", "ct0": "c"}).headers)


if __name__ == "__main__":
    unittest.main()
