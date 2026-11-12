"""Twitter / X: timelines from the GraphQL API, each category, the profile, one tweet, and the browser fallback."""

import json
import unittest
from unittest import mock

import httpx

from fakes import Seen, collect
from test_engine import EngineCase

from flnd import site as site_module
from flnd.common import auth
from flnd.engine import Engine
from flnd.session import Options
from flnd.source import FetchError, Target, TargetError
from flnd.sources.twt import QUERIES, TWITTER_BEARER, Twitter, read_timeline_page
from shared import cookies

API = "https://x.com/i/api/graphql/"
USER_ID = "42"
OTHER_ID = "77"
DAY = "2026-11-09"


def url(query):
    return f"{API}{QUERIES[query]}/{query}"


def photo(name, extension="jpg"):
    return {"type": "photo", "media_url_https": f"https://pbs.example/media/{name}.{extension}"}


def video(name):
    return {"type": "video", "video_info": {"variants": [
        {"content_type": "application/x-mpegURL", "url": f"https://video.example/{name}.m3u8"},
        {"content_type": "video/mp4", "bitrate": 1, "url": f"https://video.example/{name}_low.mp4"},
        {"content_type": "video/mp4", "bitrate": 9, "url": f"https://video.example/{name}.mp4"}]}}


def tweet(tweet_id, media, *, author=USER_ID, reply_to=None, retweet=None):
    legacy = {"id_str": tweet_id, "user_id_str": author, "created_at": "Mon Nov 09 10:00:00 +0000 2026",
              "extended_entities": {"media": media}}
    if reply_to:
        legacy["in_reply_to_status_id_str"] = reply_to
    if retweet:
        legacy["retweeted_status_result"] = {"result": retweet}
    return {"__typename": "Tweet", "rest_id": tweet_id, "legacy": legacy}


def item(result):
    return {"itemContent": {"tweet_results": {"result": result}}}


def page(*entries, cursor="next"):
    """A timeline page: its entries, then its bottom cursor."""
    added = [{"entryId": f"e{number}", "content": entry} for number, entry in enumerate(entries)]
    added.append({"entryId": "cursor-bottom-1", "content": {"cursorType": "Bottom", "value": cursor}})
    return {"data": {"user": {"result": {"timeline": {"timeline": {"instructions": [
        {"type": "TimelineAddEntries", "entries": added}]}}}}}}


def pages(*answers):
    """A fixture giving each page in turn, then an empty last page."""
    queue = list(answers)
    return lambda url, request: queue.pop(0) if queue else page()


USER = {"data": {"user": {"result": {"rest_id": USER_ID,
                                     "avatar": {"image_url": "https://pbs.example/profile_images/9/me_normal.jpg"},
                                     "banner": {"image_url": "https://pbs.example/profile_banners/42/1683721221"}}}}}


# Every form twt reads, as people write it, and what it becomes: (kind, value), or a refusal's text.
# The kinds and values are the keys of targets.db and the history, so a change here is a migration.
# A refusal is matched by a fragment both the old and the new message contain.
NOT_A_PROFILE = "(?:can't read|that's not a profile)"
ROUTES = [
    ("twt:example_handle", ("user", "example_handle")),
    ("twt:@example_handle", ("user", "example_handle")),
    ("twt:example.handle", ("user", "example.handle")),
    ("twt:x.com", ("user", "x.com")),
    ("twt:44196397", ("user", "44196397")),
    ("twt:1234567890123456789", ("user", "1234567890123456789")),
    ("https://x.com/example_handle", ("user", "example_handle")),
    ("x.com/example_handle", ("user", "example_handle")),
    ("twitter.com/example_handle", ("user", "example_handle")),
    ("https://twitter.com/example_handle/media", ("user", "example_handle")),
    ("https://www.x.com/example_handle/", ("user", "example_handle")),
    ("https://mobile.twitter.com/example_handle", ("user", "example_handle")),
    ("http://x.com/example_handle#top", ("user", "example_handle")),
    ("https://x.com/example_handle?lang=en", ("user", "example_handle")),
    ("https://x.com/@example_handle", ("user", "example_handle")),
    ("https://x.com/example.handle", ("user", "example.handle")),
    ("https://x.com/example_handle/status/abc", ("user", "example_handle")),
    ("https://x.com/example_handle/status/1790000000000000000/", ("post", "1790000000000000000")),
    ("https://x.com/example_handle/status/1790000000000000000", ("post", "1790000000000000000")),
    ("https://x.com/example_handle/status/1790000000000000000?s=20", ("post", "1790000000000000000")),
    ("https://twitter.com/example_handle/status/1790000000000000000/photo/1", ("post", "1790000000000000000")),
    ("https://www.twitter.com/example_handle/status/1790000000000000000", ("post", "1790000000000000000")),
    ("twt:x.com/example_handle/status/1790000000000000000", ("post", "1790000000000000000")),
    ("twt:twitter.com/example_handle/status/1790000000000000000", ("post", "1790000000000000000")),
    ("https://x.com/status/1790000000000000000", ("post", "1790000000000000000")),
    ("https://twitter.com/status/1790000000000000000", ("post", "1790000000000000000")),
    ("https://x.com/i/web/status/1790000000000000000", ("post", "1790000000000000000")),
    ("https://twitter.com/i/web/status/1790000000000000000", ("post", "1790000000000000000")),
    ("https://x.com/home", NOT_A_PROFILE),
    ("https://x.com/explore", NOT_A_PROFILE),
    ("https://x.com/search?q=example", NOT_A_PROFILE),
    ("https://x.com/settings", NOT_A_PROFILE),
    ("https://x.com/notifications", NOT_A_PROFILE),
    ("https://x.com/messages/1", NOT_A_PROFILE),
    ("https://x.com/hashtag/tag", NOT_A_PROFILE),
    ("https://x.com/i", NOT_A_PROFILE),
    ("https://x.com/i/flow/login", NOT_A_PROFILE),
    ("https://twitter.com/home", NOT_A_PROFILE),
    ("https://twitter.com/explore", NOT_A_PROFILE),
    ("https://twitter.com/search?q=example", NOT_A_PROFILE),
    ("https://twitter.com/settings", NOT_A_PROFILE),
    ("https://twitter.com/notifications", NOT_A_PROFILE),
    ("https://twitter.com/messages/1", NOT_A_PROFILE),
    ("https://twitter.com/hashtag/tag", NOT_A_PROFILE),
    ("https://twitter.com/i/flow/login", NOT_A_PROFILE),
    ("twt:https://x.com/explore", NOT_A_PROFILE),
    ("twt:not a name", "can't read"),
    ("twt:ex-ample", "can't read"),
    ("twt:", "can't read"),
    ("twt:example.com/someone", "can't read"),
    ("twt:https://example.com/someone", "can't read"),
    ("https://notx.com/example_handle", "can't read"),
    ("https://x.com/", "can't read"),
    ("https://x.com/example handle", "can't read"),
]


class RoutesTest(unittest.TestCase):
    def test_every_form(self):
        for text, expected in ROUTES:
            with self.subTest(text=text):
                value = text.removeprefix("twt:")
                if isinstance(expected, tuple):
                    self.assertEqual(Twitter().resolve(value), Target("twt", *expected))
                else:
                    with self.assertRaisesRegex(TargetError, f"^twt: {expected}"):
                        Twitter().resolve(value)


# The tweets of the "today's names and keys" tests, covering what a name depends on: a tweet with no date,
# media with no file (a GIF) that still count in `n`, a photo URL with no extension or in a rarer one, a retweet.
RETWEETED = tweet("500", [photo("rt")], author=OTHER_ID)
NO_DATE = tweet("700", [photo("a"), {"type": "animated_gif"}, video("c")])
del NO_DATE["legacy"]["created_at"]
SKIPS = tweet("800", [{"type": "animated_gif"}, photo("s"), {"type": "video", "video_info": {"variants": []}},
                      video("t")])
ODD = tweet("900", [{"type": "photo", "media_url_https": "https://pbs.example/media/plain"},
                    {"type": "photo", "media_url_https": "https://pbs.example/media/x.jpeg"}])
EVERYTHING = {
    url("UserByScreenName"): USER,
    url("UserOriginalsTimeline"): pages(page(
        item(tweet("100", [photo("p", "png"), video("v")])), item(tweet("101", [photo("r")], retweet=RETWEETED)),
        item(NO_DATE), item(SKIPS), item(ODD))),
    url("UserMedia"): pages(page({"items": [{"item": item(tweet("100", [photo("p", "png")]))},
                                            {"item": item(tweet("200", [video("m")]))}, {"item": item(SKIPS)}]})),
    url("UserHighlightsTweets"): pages(page(item(tweet("300", [photo("h")])))),
    url("UserRepostsTimeline"): pages(page(item(tweet("400", [], retweet=RETWEETED)),
                                           item(tweet("401", [photo("own")])))),
    url("UserRepliesTimeline"): pages(page({"items": [
        {"item": item(tweet("600", [photo("parent")], author=OTHER_ID))},
        {"item": item(tweet("601", [photo("reply")], reply_to="600"))}]})),
}
# What the `run(session)` version of twt saved for those answers, recorded through its fake session
# before the port (the history key of a profile picture was then none, which meant its path):
# (path, key) in the order saved. The photo `100_0` is on the Media tab too, and saved once. Each
# key is without the source's code, which the history adds.
TODAY = [
    ("posts/2026-11-09_100_0.png", "100_0"), ("posts/2026-11-09_100_1.mp4", "100_1"),
    ("posts/700_0.jpg", "700_0"), ("posts/700_2.mp4", "700_2"),
    ("posts/2026-11-09_800_1.jpg", "800_1"), ("posts/2026-11-09_800_3.mp4", "800_3"),
    ("posts/2026-11-09_900_0.jpg", "900_0"), ("posts/2026-11-09_900_1.jpeg", "900_1"),
    ("videos/2026-11-09_200_0.mp4", "200_0"),
    ("highlights/2026-11-09_300_0.jpg", "300_0"),
    ("reposts/2026-11-09_500_0.jpg", "500_0"),
    ("replies/2026-11-09_601_0.jpg", "601_0"),
    ("profile/avatar_9.jpg", "profile/avatar_9.jpg"), ("profile/banner_1683721221.jpg", "profile/banner_1683721221.jpg"),
]


class Feed:
    """What `watch_responses` gives: the responses the browser would have read, by the timeline watched."""

    def __init__(self, answers, match):
        self.data = [data for query, data in answers.items() if f"/{query}" == match]

    def __enter__(self):
        return iter(self.data)

    def __exit__(self, *exc_info):
        return False


def browser(answers):
    """Patch the scroll fallback to read `answers`, `{query: page}`, when a timeline's tab is watched."""
    return mock.patch("flnd.sources.twt.watch_responses", lambda site, match, scroll=False: Feed(answers, match))


def pairs(saved):
    return [(item.path, item.key) for item in saved]


def queries(seen):
    """The GraphQL query each request asked."""
    return [request_url.rsplit("/", 1)[-1] for _, request_url, _ in seen.requests]


class NamesAndKeysTest(unittest.TestCase):
    """Fixed by the rule of phase 6 step 6: no history migration, so every name and key stays as it was."""

    def test_every_category_names_and_keys_its_files_as_run_did(self):
        saved = collect(Twitter, "twt:someone", json=EVERYTHING)
        # The order differs only in the profile, now read after the timelines: it is its own category.
        self.assertEqual(sorted(pairs(saved)), sorted(TODAY))
        self.assertEqual([item.url for item in saved[:3]], [
            "https://pbs.example/media/p.png?name=orig", "https://video.example/v.mp4",
            "https://pbs.example/media/a.jpg?name=orig"])
        self.assertEqual([item.url for item in saved[-2:]], [
            "https://pbs.example/profile_images/9/me.jpg", "https://pbs.example/profile_banners/42/1683721221/1500x500"])

    def test_one_tweet_is_named_and_keyed_as_before(self):
        result = {"__typename": "TweetWithVisibilityResults", "tweet": tweet("1790", [photo("a"), video("b")])}
        seen = Seen()
        saved = collect(Twitter, "twt:https://x.com/someone/status/1790", seen=seen, json={
            url("TweetResultByRestId"): {"data": {"tweetResult": {"result": result}}}})
        self.assertEqual(pairs(saved), [(f"{DAY}_1790_0.jpg", "1790_0"), (f"{DAY}_1790_1.mp4", "1790_1")])
        self.assertEqual(json.loads(seen.requests[0][2]["params"]["variables"])["tweetId"], "1790")

    def test_the_browser_names_and_keys_as_the_api_does(self):
        answers = {"UserMedia": page(item(tweet("333", [photo("b")])))}
        with browser(answers):
            saved = collect(Twitter, "twt:someone", options=Options(categories=("posts", "media")), json={
                url("UserByScreenName"): USER, url("UserOriginalsTimeline"): FetchError("HTTP 404", status=404)})
        self.assertEqual(pairs(saved), [(f"images/{DAY}_333_0.jpg", "333_0")])


class TimelineTest(unittest.TestCase):
    def test_grid_items_added_to_a_module_are_read(self):
        data = {"data": {"user": {"result": {"timeline": {"timeline": {"instructions": [
            {"type": "TimelineAddToModule", "moduleItems": [{"entryId": "m", "item": item(tweet("1", [photo("a")]))}]},
            {"type": "TimelinePinEntry", "entry": {"entryId": "p", "content": item(tweet("2", [photo("b")]))}},
        ]}}}}}}
        tweets, cursor = read_timeline_page(data)
        self.assertEqual([found["rest_id"] for found in tweets], ["1", "2"])
        self.assertIsNone(cursor)

    def test_the_profile_is_read_once_and_asked_for_by_its_name(self):
        seen = Seen()
        collect(Twitter, "twt:someone", options=Options(categories=("profile",)), json={url("UserByScreenName"): USER},
                seen=seen)
        self.assertEqual(queries(seen), ["UserByScreenName"])

    def test_a_number_is_a_user_id(self):
        seen = Seen()
        collect(Twitter, "twt:42", options=Options(categories=("profile",)), json={url("UserByRestId"): USER}, seen=seen)
        self.assertEqual(json.loads(seen.requests[0][2]["params"]["variables"]), {"userId": "42"})

    def test_pages_are_followed_until_one_has_no_new_tweets(self):
        seen = Seen()
        saved = collect(Twitter, "twt:42", options=Options(categories=("posts",)), seen=seen, json={
            url("UserByRestId"): USER,
            # Every page repeats the pinned tweet.
            url("UserOriginalsTimeline"): pages(page(item(tweet("1", [photo("a")])), cursor="c1"),
                                                page(item(tweet("1", [photo("a")])), item(tweet("2", [photo("b")])),
                                                     cursor="c2"),
                                                page(item(tweet("1", [photo("a")])), cursor="c3")),
        })
        self.assertEqual(len(saved), 2)
        variables = [json.loads(request["params"]["variables"]) for _, _, request in seen.requests[1:]]
        self.assertEqual([found.get("cursor") for found in variables], [None, "c1", "c2"])
        # 50 a page, as the web app asks.
        self.assertEqual({found["count"] for found in variables}, {50})
        self.assertEqual({found["userId"] for found in variables}, {USER_ID})
        self.assertIn("posts: 3 pages read", seen.messages)

    def test_without_from_every_timeline_is_read_in_its_order(self):
        seen = Seen()
        collect(Twitter, "twt:someone", seen=seen, json={
            url("UserByScreenName"): USER, url("UserOriginalsTimeline"): page(), url("UserMedia"): page(),
            url("UserHighlightsTweets"): page(), url("UserRepostsTimeline"): page(), url("UserRepliesTimeline"): page()})
        self.assertEqual(queries(seen), ["UserByScreenName", "UserOriginalsTimeline", "UserMedia",
                                         "UserHighlightsTweets", "UserRepostsTimeline", "UserRepliesTimeline"])
        self.assertEqual(seen.visited, [])

    def test_a_refused_timeline_switches_to_the_browser_for_it_and_the_rest(self):
        seen = Seen()
        answers = {"UserMedia": page(item(tweet("333", [photo("b")])))}
        with browser(answers):
            saved = collect(Twitter, "twt:someone", options=Options(categories=("posts", "media")), seen=seen, json={
                url("UserByScreenName"): USER, url("UserOriginalsTimeline"): FetchError("HTTP 404", status=404)})
        self.assertEqual(seen.visited, ["https://x.com/someone", "https://x.com/someone/media"])
        # The media timeline was never asked of the API: the browser had taken over.
        self.assertEqual(queries(seen), ["UserByScreenName", "UserOriginalsTimeline"])
        self.assertEqual([item.path for item in saved], [f"images/{DAY}_333_0.jpg"])
        self.assertTrue(any(line.startswith("! posts: HTTP 404") and line.endswith("; reading it in the browser")
                            for line in seen.messages), seen.messages)

    def test_no_answer_and_no_json_are_not_refusals(self):
        seen = Seen()
        with browser({}):
            collect(Twitter, "twt:someone", options=Options(categories=("posts", "media")), seen=seen,
                    json={url("UserByScreenName"): USER, url("UserOriginalsTimeline"): FetchError("no answer"),
                          url("UserMedia"): pages(page())})
        self.assertEqual(seen.visited, [])
        self.assertIn("! posts: no answer", seen.messages)
        seen = Seen()
        collect(Twitter, "twt:someone", options=Options(categories=("posts",)), seen=seen,
                json={url("UserByScreenName"): USER}, html={url("UserOriginalsTimeline"): "<html>"})
        self.assertEqual(seen.visited, [])
        self.assertTrue(any(line.startswith("! posts: ") for line in seen.messages), seen.messages)

    def test_an_unknown_user_is_read_in_the_browser(self):
        seen = Seen()
        with browser({}):
            saved = collect(Twitter, "twt:someone", options=Options(categories=("posts", "profile")), seen=seen)
        self.assertEqual(seen.visited, ["https://x.com/someone"])
        self.assertEqual(saved, [])
        self.assertIn("! the API can't find @someone; reading the profile in the browser", seen.messages)

    def test_a_number_the_api_does_not_know_is_opened_by_its_id(self):
        seen = Seen()
        with browser({}):
            collect(Twitter, "twt:42", options=Options(categories=("posts",)), seen=seen)
        self.assertEqual(seen.visited, ["https://x.com/i/user/42"])

    def test_the_browser_keeps_the_users_replies_only_when_the_user_is_known(self):
        reply = page(item(tweet("601", [photo("reply")], reply_to="600")))
        with browser({"UserRepliesTimeline": reply}):
            known = collect(Twitter, "twt:someone", options=Options(categories=("replies",)), json={
                url("UserByScreenName"): USER, url("UserRepliesTimeline"): FetchError("HTTP 404", status=404)})
            unknown = collect(Twitter, "twt:someone", options=Options(categories=("replies",)))
        self.assertEqual([item.path for item in known], [f"replies/{DAY}_601_0.jpg"])
        self.assertEqual(unknown, [])

    def test_only_the_timelines_with_a_known_order_stop_early(self):
        flags = {category.name: category.newest_first for category in Twitter.routes[1].categories}
        self.assertEqual(flags, {"posts": True, "media": True, "highlights": False, "reposts": False,
                                 "replies": True, "profile": False})


class OneTweetTest(unittest.TestCase):
    def test_a_tweet_goes_into_the_targets_own_folder(self):
        result = {"__typename": "TweetWithVisibilityResults", "tweet": tweet("1790", [photo("a")])}
        saved = collect(Twitter, "twt:https://x.com/someone/status/1790", json={
            url("TweetResultByRestId"): {"data": {"tweetResult": {"result": result}}}})
        self.assertEqual([item.post.category for item in saved], [""])
        self.assertEqual([item.path for item in saved], [f"{DAY}_1790_0.jpg"])

    def test_a_missing_tweet_is_a_warning(self):
        seen = Seen()
        collect(Twitter, "twt:https://x.com/someone/status/1790", seen=seen,
                json={url("TweetResultByRestId"): {"data": {"tweetResult": {}}}})
        self.assertEqual(seen.messages, ["! tweet 1790 isn't there, or isn't visible to this login"])

    def test_a_refused_tweet_is_a_warning(self):
        seen = Seen()
        collect(Twitter, "twt:https://x.com/someone/status/1790", seen=seen)
        self.assertEqual(len(seen.messages), 1)
        self.assertTrue(seen.messages[0].startswith("! tweet 1790: HTTP 404"), seen.messages)


class PrepareTest(unittest.TestCase):
    """Fix 6: the API's headers come from the live cookies, and there is no `cookie` header."""

    def prepared(self, target, cookie_values):
        return Twitter().prepare(httpx.Request("GET", target), cookie_values)

    def test_the_api_headers_come_from_the_live_cookies(self):
        request = self.prepared("https://x.com/i/api/graphql/q/UserMedia", {"auth_token": "a", "ct0": "c"})
        self.assertEqual((request.headers["authorization"], request.headers["x-csrf-token"],
                          request.headers["x-twitter-auth-type"]), (TWITTER_BEARER, "c", "OAuth2Session"))
        self.assertNotIn("cookie", request.headers)
        # A rotated token is the next request's.
        self.assertEqual(self.prepared("https://x.com/i/api/graphql/q/UserMedia", {"ct0": "d"}).headers["x-csrf-token"], "d")
        self.assertNotIn("x-twitter-auth-type", self.prepared("https://x.com/", {}).headers)


class LoginStaysAtHomeTest(EngineCase):
    """Through the engine: what the API and the media servers each receive, with a `ct0` that rotates."""

    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(site_module, "_sleep", self.sleep),
                        mock.patch.object(site_module, "_clock", lambda: self.now)):
            patcher.start()
            self.addCleanup(patcher.stop)
        jar = [{"name": name, "value": value, "domain": ".x.com", "path": "/"}
               for name, value in {"auth_token": "secret", "ct0": "old"}.items()]
        cookies.Jar(jar).save(auth.cookie_file(self.home, "twt"))
        self.routes[url("UserByScreenName")] = httpx.Response(
            200, json=USER, headers={"set-cookie": "ct0=new; Domain=x.com; Path=/"})
        timeline = page(item(tweet("100", [
            {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/p.jpg"},
            {"type": "video", "video_info": {"variants": [
                {"content_type": "video/mp4", "bitrate": 9, "url": "https://video.twimg.com/v/v.mp4"}]}}])))
        self.routes[url("UserOriginalsTimeline")] = lambda request: httpx.Response(200, json=timeline)

    def test_media_servers_never_see_the_login_and_the_api_sees_the_refreshed_token(self):
        engine = Engine(home=self.home, sources={"twt": Twitter})
        engine.run(engine.resolve(["twt:someone"]), Options(categories=("posts",)))
        api = [request for request in self.requests if request.url.host == "x.com"]
        media = [request for request in self.requests if request.url.host.endswith("twimg.com")]
        self.assertEqual(sorted(request.url.host for request in media), ["pbs.twimg.com", "video.twimg.com"])
        for request in media:
            with self.subTest(host=request.url.host):
                self.assertNotIn("authorization", request.headers)
                self.assertNotIn("x-csrf-token", request.headers)
                self.assertNotIn("cookie", request.headers)
        self.assertEqual({request.headers["authorization"] for request in api}, {TWITTER_BEARER})
        # The first request carries the token it was saved with; x.com rotated it, and the rest carry the new one.
        self.assertEqual([request.headers["x-csrf-token"] for request in api], ["old", "new", "new"])
        self.assertIn("ct0=new", api[1].headers["cookie"])
        self.assertNotIn("ct0=old", api[1].headers["cookie"])
        self.assertTrue(any(path.name.endswith("_100_0.jpg") for path in self.target_dir("twt", "someone").rglob("*")))


if __name__ == "__main__":
    unittest.main()


