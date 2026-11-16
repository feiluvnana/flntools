"""Twitter / X: timelines scrolled in the browser, each category, the profile, one tweet, and stale
query ids refreshed from x.com's script.

What is faked: the API's and x.com's answers (`FakeSite` through `collect`), and the browser's scroll
(`browser`). No network.
"""

import json
import unittest
from datetime import date
from unittest import mock

import httpx

from fakes import Seen, collect
from test_engine import EngineCase

from flnd.common import auth
from flnd.options import Options
from flnd.source import FetchError, LoginError, Target, TargetError
from flnd.sources.twt import QUERIES, TWITTER_BEARER, Twitter, read_timeline_page, tweet_posts
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


def sizes(url):
    """A photo's URL at each size X gives, best first: its mirrors (fixes #8)."""
    return tuple(f"{url}?name={size}" for size in ("orig", "4096x4096", "large", "medium"))


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
    ("https://x.com/i/user/44196397", ("user", "44196397")),
    ("https://twitter.com/i/user/44196397", ("user", "44196397")),
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
    ("https://x.com/intent/tweet", NOT_A_PROFILE),
    ("https://x.com/compose/post", NOT_A_PROFILE),
    ("https://x.com/login", NOT_A_PROFILE),
    ("https://twitter.com/home", NOT_A_PROFILE),
    ("https://twitter.com/explore", NOT_A_PROFILE),
    ("https://twitter.com/search?q=example", NOT_A_PROFILE),
    ("https://twitter.com/settings", NOT_A_PROFILE),
    ("https://twitter.com/notifications", NOT_A_PROFILE),
    ("https://twitter.com/messages/1", NOT_A_PROFILE),
    ("https://twitter.com/hashtag/tag", NOT_A_PROFILE),
    ("https://twitter.com/i/flow/login", NOT_A_PROFILE),
    ("https://twitter.com/intent/tweet", NOT_A_PROFILE),
    ("https://twitter.com/compose/post", NOT_A_PROFILE),
    ("https://twitter.com/login", NOT_A_PROFILE),
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
EVERYTHING_FEEDS = {
    "UserOriginalsTimeline": page(
        item(tweet("100", [photo("p", "png"), video("v")])), item(tweet("101", [photo("r")], retweet=RETWEETED)),
        item(NO_DATE), item(SKIPS), item(ODD)),
    "UserMedia": page({"items": [{"item": item(tweet("100", [photo("p", "png")]))},
                                 {"item": item(tweet("200", [video("m")]))}, {"item": item(SKIPS)}]}),
    "UserHighlightsTweets": page(item(tweet("300", [photo("h")]))),
    "UserRepostsTimeline": page(item(tweet("400", [], retweet=RETWEETED)),
                                item(tweet("401", [photo("own")]))),
    "UserRepliesTimeline": page({"items": [
        {"item": item(tweet("600", [photo("parent")], author=OTHER_ID))},
        {"item": item(tweet("601", [photo("reply")], reply_to="600"))}]}),
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
        raw = [data for query, data in answers.items() if f"/{query}" == match]
        self.data = [item for entry in raw for item in (entry if isinstance(entry, list) else [entry])]
        self.iter = iter(self.data)

    def __enter__(self):
        return self.iter

    def __exit__(self, *exc_info):
        return False


def browser(answers):
    """Patch the scroll to read `answers`, `{query: page | [pages]}`, when a timeline's tab is watched."""
    return mock.patch("flnd.sources.twt.watch_responses", lambda site, match, scroll=False: Feed(answers, match))


def pairs(saved):
    return [(item.path, item.key) for item in saved]


def queries(seen):
    """The GraphQL query each request asked; x.com's page and script, read for a refresh, left out."""
    return [request_url.rsplit("/", 1)[-1] for _, request_url, _ in seen.requests if request_url.startswith(API)]


class NamesAndKeysTest(unittest.TestCase):
    """Fixed by the rule of phase 6 step 6: no history migration, so every name and key stays as it was."""

    def test_every_category_names_and_keys_its_files_as_run_did(self):
        with browser(EVERYTHING_FEEDS):
            saved = collect(Twitter, "twt:someone", json={url("UserByScreenName"): USER})
        # The order differs only in the profile, now read after the timelines: it is its own category.
        self.assertEqual(sorted(pairs(saved)), sorted(TODAY))
        self.assertEqual([item.url for item in saved[:3]], [
            sizes("https://pbs.example/media/p.png"), "https://video.example/v.mp4",
            sizes("https://pbs.example/media/a.jpg")])
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
            saved = collect(Twitter, "twt:someone", options=Options(categories=("media",)), json={
                url("UserByScreenName"): USER})
        self.assertEqual(pairs(saved), [(f"images/{DAY}_333_0.jpg", "333_0")])


class QuotedAndCardTest(unittest.TestCase):
    """fixes #11: a quoted tweet's media and a card's image are files of the quoting post, keyed apart."""

    def test_quoted_media_and_the_card_image_come_with_the_post(self):
        quoting = tweet("1", [photo("own")])
        quoting["quoted_status_result"] = {"result": tweet("2", [photo("quoted"), video("clip")], author="7")}
        quoting["card"] = {"legacy": {"binding_values": [
            {"key": "thumbnail_image_original", "value": {"image_value": {"url": "https://pbs.example/card_img/t"}}},
            {"key": "photo_image_full_size_original",
             "value": {"image_value": {"url": "https://pbs.example/card_img/full?format=jpg&name=orig"}}}]}}
        posts = list(tweet_posts(quoting, "posts", USER_ID))
        self.assertEqual([(post.id, post.fields["n"]) for post in posts], [("1", 0), ("1", "q0"), ("1", "q1"), ("1", "card")])
        f1 = list(posts[1].files() if callable(posts[1].files) else posts[1].files)
        f2 = list(posts[2].files() if callable(posts[2].files) else posts[2].files)
        f3 = list(posts[3].files() if callable(posts[3].files) else posts[3].files)
        self.assertEqual(f1[0].url, sizes("https://pbs.example/media/quoted.jpg"))
        self.assertEqual(f2[0].url, "https://video.example/clip.mp4")
        card = f3[0]
        self.assertEqual((card.url, card.name), ("https://pbs.example/card_img/full?format=jpg&name=orig",
                                                 "2026-11-09_1_card.jpg"))

    def test_a_tweet_without_a_quote_or_card_has_only_its_own(self):
        self.assertEqual([post.fields["n"] for post in tweet_posts(tweet("1", [photo("a")]), "posts", USER_ID)], [0])


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

    def test_browser_scroll_collects_pages_until_no_new_tweets(self):
        seen = Seen()
        p1 = page(item(tweet("1", [photo("a")])), cursor="c1")
        p2 = page(item(tweet("1", [photo("a")])), item(tweet("2", [photo("b")])), cursor="c2")
        p3 = page(item(tweet("1", [photo("a")])), cursor="c3")
        p4 = page(item(tweet("3", [photo("c")])), cursor="c4")
        answers = {"UserOriginalsTimeline": [p1, p2, p3, p4]}
        with browser(answers):
            saved = collect(Twitter, "twt:42", options=Options(categories=("posts",)), seen=seen,
                            json={url("UserByRestId"): USER})
        self.assertEqual(len(saved), 2)
        self.assertEqual(seen.visited, ["https://x.com/i/user/42"])

    def test_an_error_page_or_a_bad_date_does_not_end_the_scroll(self):
        seen = Seen()
        p1 = page(item(tweet("1", [photo("a")])), cursor="c1")
        throttled = {"errors": [{"message": "Rate limit exceeded", "code": 88}]}
        undated = tweet("2", [photo("b")])
        undated["legacy"]["created_at"] = "yesterday"
        p2 = page(item(undated), cursor="c2")
        p3 = page(item(tweet("3", [photo("c")])), cursor="c3")
        with browser({"UserOriginalsTimeline": [p1, throttled, p2, p3]}):
            saved = collect(Twitter, "twt:42", options=Options(categories=("posts",)), seen=seen,
                            json={url("UserByRestId"): USER})
        self.assertEqual(sorted(s.post.id for s in saved), ["1", "2", "3"])

    def test_early_stop_on_posts_after_five_consecutive_old_tweets(self):
        old_date = "Mon Nov 02 10:00:00 +0000 2026"
        old_tweets = [item({**tweet(str(10 + i), [photo(f"o{i}")]),
                            "legacy": {**tweet(str(10 + i), [photo(f"o{i}")])["legacy"], "created_at": old_date}})
                      for i in range(5)]
        p1 = page(item(tweet("1", [photo("new")])))
        p2 = page(*old_tweets)
        p3 = page(item(tweet("99", [photo("later")])))
        consumed = []

        class RecordingFeed(Feed):
            def __enter__(self):
                def gen():
                    for entry in self.data:
                        consumed.append(entry)
                        yield entry
                return gen()

        with mock.patch("flnd.sources.twt.watch_responses",
                        lambda site, match, scroll=False: RecordingFeed({"UserOriginalsTimeline": [p1, p2, p3]}, match)):
            saved = collect(Twitter, "twt:someone",
                            options=Options(categories=("posts",), since=date(2026, 11, 8)),
                            json={url("UserByScreenName"): USER})
        self.assertEqual([s.post.id for s in saved], ["1"])
        self.assertEqual(len(consumed), 2)

    def test_early_stop_on_posts_when_caught_up(self):
        p1 = page(item(tweet("1", [photo("a")])))
        p2 = page(item(tweet("2", [photo("b")])))
        p3 = page(item(tweet("3", [photo("c")])))
        consumed = []

        class RecordingFeed(Feed):
            def __enter__(self):
                def gen():
                    for entry in self.data:
                        consumed.append(entry)
                        yield entry
                return gen()

        with (
            mock.patch("flnd.sources.twt.watch_responses",
                       lambda site, match, scroll=False: RecordingFeed({"UserOriginalsTimeline": [p1, p2, p3]}, match)),
            mock.patch("flnd.sources.twt.caught_up", side_effect=lambda tid, **_: tid == "2"),
        ):
            saved = collect(Twitter, "twt:someone", options=Options(categories=("posts",)),
                            json={url("UserByScreenName"): USER})
        self.assertEqual([s.post.id for s in saved], ["1", "2"])
        self.assertEqual(len(consumed), 2)

    def test_without_from_every_timeline_is_read_in_its_order(self):
        seen = Seen()
        with browser({}):
            collect(Twitter, "twt:someone", seen=seen, json={url("UserByScreenName"): USER})
        self.assertEqual(queries(seen), ["UserByScreenName"])
        self.assertEqual(seen.visited, [
            "https://x.com/someone", "https://x.com/someone/media", "https://x.com/someone/highlights",
            "https://x.com/someone/reposts", "https://x.com/someone/with_replies",
        ])

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
                url("UserByScreenName"): USER})
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
        # A 404 may be a stale id: x.com's script is asked once (here it can't be read), then the tweet is warned of.
        self.assertEqual(len(seen.messages), 2)
        self.assertTrue(seen.messages[0].startswith("! the API's query ids look stale"), seen.messages)
        self.assertTrue(seen.messages[1].startswith("! tweet 1790: HTTP 404"), seen.messages)


HOME = "https://x.com/"
SCRIPT = "https://abs.twimg.com/responsive-web/client-web/main.abc123de.js"
# x.com's page: its main script, and the feature switches' values (the user's config after the defaults wins).
HOME_PAGE = (f'<html><script src="{SCRIPT}"></script><script>window.__INITIAL_STATE__={{"featureSwitch":'
             '{"defaultConfig":{"rweb_video_screen_enabled":{"value":false},"view_counts_everywhere_api_enabled":'
             '{"value":true},"longform_notetweets_inline_media_enabled":{"value":false}},"user":{"config":'
             '{"longform_notetweets_inline_media_enabled":{"value":true}}}}};</script></html>')
# The web app's main script, as its bundler writes each operation.
MAIN_JS = ('e.exports={queryId:"NEWmedia42",operationName:"UserMedia",operationType:"query",metadata:{featureSwitches:'
           '["rweb_video_screen_enabled","view_counts_everywhere_api_enabled","longform_notetweets_inline_media_enabled",'
           '"not_in_the_page"],fieldToggles:["withArticlePlainText"]}};'
           'e.exports={queryId:"NEWuser42",operationName:"UserByScreenName",operationType:"query",metadata:'
           '{featureSwitches:["rweb_video_screen_enabled","view_counts_everywhere_api_enabled",'
           '"longform_notetweets_inline_media_enabled","not_in_the_page"],fieldToggles:[]}};'
           'e.exports={queryId:"other",operationName:"SomethingElse",operationType:"mutation",metadata:'
           '{featureSwitches:[],fieldToggles:[]}}')


class RefreshTest(unittest.TestCase):
    """A stale query id is refreshed from x.com's own script, once, before falling back."""

    def run_user(self, refreshed_user, *, home=HOME_PAGE, script=MAIN_JS, seen=None):
        with browser({}):
            return collect(Twitter, "twt:someone", options=Options(categories=("profile",)), seen=seen,
                           json={url("UserByScreenName"): FetchError("HTTP 400", status=400),
                                 f"{API}NEWuser42/UserByScreenName": refreshed_user},
                           html={HOME: home, SCRIPT: script})

    def test_a_stale_id_is_refreshed_and_the_query_sent_again_with_the_web_apps_flags(self):
        seen = Seen()
        saved = self.run_user(USER, seen=seen)
        self.assertEqual([item.path for item in saved], ["profile/avatar_9.jpg", "profile/banner_1683721221.jpg"])
        self.assertEqual(seen.visited, [])
        urls = [request_url for _, request_url, _ in seen.requests]
        self.assertEqual(urls[1:4], [HOME, SCRIPT, f"{API}NEWuser42/UserByScreenName"])
        features = json.loads(seen.requests[3][2]["params"]["features"])
        self.assertEqual(features, {"rweb_video_screen_enabled": False, "view_counts_everywhere_api_enabled": True,
                                    "longform_notetweets_inline_media_enabled": True, "not_in_the_page": False})
        self.assertIn("refreshed 2 query ids from x.com's script", seen.messages)

    def test_the_refreshed_table_is_kept_for_the_next_run(self):
        from fakes import FakeSite
        source = Twitter()
        site = FakeSite(Target("twt", "user", "someone"), html={HOME: HOME_PAGE, SCRIPT: MAIN_JS})
        self.assertTrue(source.graph.refresh(site))
        self.assertEqual(site.state["graphql.queries"]["UserMedia"]["id"], "NEWmedia42")
        later = Twitter()
        next_site = FakeSite(Target("twt", "user", "someone"), json={f"{API}NEWuser42/UserByScreenName": USER})
        next_site.state.update(site.state)
        self.assertEqual(later.graph.get(next_site, "UserByScreenName", {"screen_name": "someone"}), USER)

    def test_a_refresh_that_doesnt_help_falls_back_to_browser_for_user(self):
        seen = Seen()
        saved = self.run_user(FetchError("HTTP 400", status=400), seen=seen)
        self.assertEqual(saved, [])
        self.assertIn("! the API can't find @someone; reading the profile in the browser", seen.messages)

    def test_a_script_without_the_queries_is_a_warning_and_falls_back(self):
        seen = Seen()
        self.run_user(USER, script="nothing here", seen=seen)
        self.assertIn("! the API's query ids look stale, and x.com's script lists none of them", seen.messages)

    def test_a_refresh_is_tried_once_a_run(self):
        seen = Seen()
        collect(Twitter, "twt:https://x.com/someone/status/1", seen=seen,
                json={url("TweetResultByRestId"): FetchError("HTTP 404", status=404)})
        collect(Twitter, "twt:someone", options=Options(categories=("profile",)), seen=seen,
                json={url("UserByScreenName"): FetchError("HTTP 404", status=404)})
        self.assertEqual([u for _, u, _ in seen.requests].count(HOME), 2)

    def test_a_refusal_that_isnt_stale_is_not_refreshed(self):
        seen = Seen()
        with browser({}):
            collect(Twitter, "twt:someone", options=Options(categories=("posts",)), seen=seen,
                    json={url("UserByScreenName"): FetchError("HTTP 403", status=403)})
        self.assertNotIn(HOME, [u for _, u, _ in seen.requests])


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

    def test_the_page_and_its_script_get_no_api_headers(self):
        # 2026-11, live: x.com answered the bearer token on its home page with 401, so no refresh could read the ids.
        request = self.prepared("https://x.com/", {"auth_token": "a", "ct0": "c"})
        self.assertEqual([name for name in ("authorization", "x-csrf-token", "x-twitter-auth-type")
                          if name in request.headers], [])


class LoginStaysAtHomeTest(EngineCase):
    """Through the engine: what the API and the media servers each receive, with a `ct0` that rotates."""

    def setUp(self):
        super().setUp()
        jar = [{"name": name, "value": value, "domain": ".x.com", "path": "/"}
               for name, value in {"auth_token": "secret", "ct0": "old"}.items()]
        auth.save(self.home, "twt", cookies.Jar(jar))
        self.routes[url("UserByScreenName")] = httpx.Response(
            200, json=USER, headers={"set-cookie": "ct0=new; Domain=x.com; Path=/"})
        self.routes[url("TweetResultByRestId")] = lambda request: httpx.Response(
            200, json={"data": {"tweetResult": {"result": tweet("100", [
                {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/p.jpg"},
                {"type": "video", "video_info": {"variants": [
                    {"content_type": "video/mp4", "bitrate": 9, "url": "https://video.twimg.com/v/v.mp4"}]}}])}}})

    def test_media_servers_never_see_the_login_and_the_api_sees_the_refreshed_token(self):
        self.run_engine(Twitter, targets=["twt:someone", "twt:https://x.com/someone/status/100"],
                        options=Options(categories=("profile",)))
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
        tokens = [request.headers["x-csrf-token"] for request in api]
        self.assertEqual((tokens[0], set(tokens[1:]), len(tokens) >= 2), ("old", {"new"}, True))
        self.assertIn("ct0=new", api[1].headers["cookie"])
        self.assertNotIn("ct0=old", api[1].headers["cookie"])
        self.assertTrue(any(path.name.endswith("_100_0.jpg") for path in self.target_dir("twt", "100").rglob("*")))

    def test_read_browser_login_wall_raises_login_error(self):
        from fakes import FakeSite
        from flnd.sources.twt import Account, Timeline

        site = FakeSite(Target("twt", "user", "example_handle"),
                        pages={"https://x.com/example_handle": "<html></html>"})
        with mock.patch.object(FakeSite, "goto",
                               side_effect=lambda url: (site._seen.visited.append("https://x.com/i/flow/login"), True)[1]):
            with self.assertRaisesRegex(LoginError, "flnd auth login twt"):
                list(Twitter().read_browser(
                    site, Account("example_handle", {"rest_id": "42"}), Timeline("posts", "UserTweets", ""),
                ))


    def test_a_handle_that_starts_like_the_login_path_is_read(self):
        from fakes import FakeSite
        from flnd.sources.twt import Account, Timeline

        site = FakeSite(Target("twt", "user", "login_example"),
                        pages={"https://x.com/login_example": "<html></html>"})
        # Warming the later tabs comes right after the wall check, so reaching it means none was seen.
        with mock.patch("flnd.sources.twt.warm_tabs", side_effect=LookupError("past the wall")):
            with self.assertRaisesRegex(LookupError, "past the wall"):
                list(Twitter().read_browser(
                    site, Account("login_example", {"rest_id": "42"}), Timeline("posts", "UserTweets", ""),
                ))

if __name__ == "__main__":
    unittest.main()


