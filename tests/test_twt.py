"""Twitter / X: timelines from the GraphQL API, each category, the profile, one tweet, and the browser fallback."""

import json
import unittest

from fakes import FakeSession

from flnd.session import Options
from flnd.sources.base import FetchError
from flnd.sources.twt import QUERIES, Twitter, read_timeline_page
from flnd.target import Target

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


class ResolveTest(unittest.TestCase):
    def test_status_links_are_one_tweet(self):
        for text in ("https://x.com/example_handle/status/1790000000000000000",
                     "https://twitter.com/example_handle/status/1790000000000000000/photo/1",
                     "x.com/i/web/status/1790000000000000000"):
            with self.subTest(text=text):
                self.assertEqual(Twitter().resolve(text), Target("twt", "post", "1790000000000000000"))

    def test_a_number_is_still_a_user(self):
        self.assertEqual(Twitter().resolve("1790000000000000000"), Target("twt", "user", "1790000000000000000"))


class TimelineTest(unittest.TestCase):
    def test_grid_items_added_to_a_module_are_read(self):
        data = {"data": {"user": {"result": {"timeline": {"timeline": {"instructions": [
            {"type": "TimelineAddToModule", "moduleItems": [{"entryId": "m", "item": item(tweet("1", [photo("a")]))}]},
            {"type": "TimelinePinEntry", "entry": {"entryId": "p", "content": item(tweet("2", [photo("b")]))}},
        ]}}}}}}
        tweets, cursor = read_timeline_page(data)
        self.assertEqual([found["rest_id"] for found in tweets], ["1", "2"])
        self.assertIsNone(cursor)

    def test_every_category_from_the_api(self):
        original = tweet("500", [photo("rt")], author=OTHER_ID)
        session = FakeSession(Target("twt", "user", "someone"), options=Options(categories=("all",)), json={
            url("UserByScreenName"): USER,
            url("UserOriginalsTimeline"): pages(page(item(tweet("100", [photo("p", "png"), video("v")])),
                                                     item(tweet("101", [photo("r")], retweet=original)))),
            # The Media tab lists the same photo again, which is saved once.
            url("UserMedia"): pages(page({"items": [{"item": item(tweet("100", [photo("p", "png")]))},
                                                    {"item": item(tweet("200", [video("m")]))}]})),
            url("UserHighlightsTweets"): pages(page(item(tweet("300", [photo("h")])))),
            url("UserRepostsTimeline"): pages(page(item(tweet("400", [], retweet=original)),
                                                   item(tweet("401", [photo("own")])))),
            url("UserRepliesTimeline"): pages(page({"items": [
                {"item": item(tweet("600", [photo("parent")], author=OTHER_ID))},
                {"item": item(tweet("601", [photo("reply")], reply_to="600"))}]})),
        })
        Twitter().run(session)
        self.assertEqual(session.paths(), [
            "profile/avatar_9.jpg", "profile/banner_1683721221.jpg",
            f"posts/{DAY}_100_0.png", f"posts/{DAY}_100_1.mp4", f"videos/{DAY}_200_0.mp4",
            f"highlights/{DAY}_300_0.jpg", f"reposts/{DAY}_500_0.jpg", f"replies/{DAY}_601_0.jpg"])
        self.assertEqual([item["source"] for item in session.saved[:4]], [
            "https://pbs.example/profile_images/9/me.jpg", "https://pbs.example/profile_banners/42/1683721221/1500x500",
            "https://pbs.example/media/p.png?name=orig", "https://video.example/v.mp4"])
        self.assertEqual(session.saved[2]["key"], "100_0")
        variables = json.loads(session.requests[1][2]["params"]["variables"])
        self.assertEqual(variables["userId"], USER_ID)
        self.assertEqual(session.visited, [])

    def test_pages_are_followed_until_one_has_no_tweets(self):
        session = FakeSession(Target("twt", "user", "42"), options=Options(categories=("posts",)), json={
            url("UserByRestId"): USER,
            # Every page repeats the pinned tweet.
            url("UserOriginalsTimeline"): pages(page(item(tweet("1", [photo("a")])), cursor="c1"),
                                                page(item(tweet("1", [photo("a")])), item(tweet("2", [photo("b")])),
                                                     cursor="c2"),
                                                page(item(tweet("1", [photo("a")])), cursor="c3")),
        })
        Twitter().run(session)
        self.assertEqual(len(session.paths()), 2)
        cursors = [json.loads(request["params"]["variables"]).get("cursor") for _, _, request in session.requests[1:]]
        self.assertEqual(cursors, [None, "c1", "c2"])
        self.assertIn("posts: 3 pages read", session.messages)

    def test_defaults_leave_out_reposts_and_replies(self):
        session = FakeSession(Target("twt", "user", "someone"), json={
            url("UserByScreenName"): USER, url("UserOriginalsTimeline"): page(), url("UserMedia"): page(),
            url("UserHighlightsTweets"): page()})
        Twitter().run(session)
        self.assertEqual([request_url.rsplit("/", 1)[-1] for _, request_url, _ in session.requests],
                         ["UserByScreenName", "UserOriginalsTimeline", "UserMedia", "UserHighlightsTweets"])

    def test_a_refused_timeline_switches_to_the_browser(self):
        session = FakeSession(Target("twt", "user", "someone"), options=Options(categories=("posts", "media")), json={
            url("UserByScreenName"): USER, url("UserOriginalsTimeline"): FetchError("HTTP 404", status=404),
        }, on_scroll=lambda session: session.folder == "media" and session.respond(
            "https://x.com/i/api/graphql/x/UserMedia?variables=", page(item(tweet("333", [photo("b")])))))
        Twitter().run(session)
        self.assertEqual(session.visited, ["https://x.com/someone", "https://x.com/someone/media"])
        self.assertEqual(session.paths(), [f"images/{DAY}_333_0.jpg"])

    def test_an_unknown_user_is_read_in_the_browser(self):
        session = FakeSession(Target("twt", "user", "someone"), options=Options(categories=("posts",)))
        Twitter().run(session)
        self.assertEqual(session.visited, ["https://x.com/someone"])

    def test_headers_carry_the_session(self):
        headers = Twitter().headers({"auth_token": "a", "ct0": "c"})
        self.assertEqual((headers["x-csrf-token"], headers["x-twitter-auth-type"], headers["cookie"]),
                         ("c", "OAuth2Session", "auth_token=a; ct0=c"))
        self.assertEqual(Twitter().headers({})["x-twitter-auth-type"], "")


class OneTweetTest(unittest.TestCase):
    def test_a_tweet_by_its_id(self):
        result = {"__typename": "TweetWithVisibilityResults", "tweet": tweet("1790", [photo("a"), video("b")])}
        session = FakeSession(Target("twt", "post", "1790"), json={
            url("TweetResultByRestId"): {"data": {"tweetResult": {"result": result}}}})
        Twitter().run(session)
        self.assertEqual(session.paths(), [f"{DAY}_1790_0.jpg", f"{DAY}_1790_1.mp4"])
        self.assertEqual(json.loads(session.requests[0][2]["params"]["variables"])["tweetId"], "1790")

    def test_a_missing_tweet_is_a_warning(self):
        session = FakeSession(Target("twt", "post", "1790"), json={
            url("TweetResultByRestId"): {"data": {"tweetResult": {}}}})
        Twitter().run(session)
        self.assertEqual(session.messages, ["! tweet 1790 isn't there, or isn't visible to this login"])


if __name__ == "__main__":
    unittest.main()
