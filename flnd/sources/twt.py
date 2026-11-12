"""Twitter / X: a user's posts, media, highlights, reposts, replies and profile pictures, or one tweet.

  - Each timeline is read from the GraphQL API to its end, page by page. When the API refuses
    (an old query id, say), a browser scrolls the same tab and reads the same responses.
  - Photos are saved at full resolution (`name=orig`), and videos as the highest-bitrate MP4.
  - A tweet's file found on two timelines (most posts are on the Media tab too) is saved once.
  - Reposts are the media of the tweets the user retweeted; replies, the user's own replies.

Folder layout, under `.flnd/downloads/twt/<username>/`:
  posts/, highlights/, reposts/, replies/   <YYYY-MM-DD>_<tweet_id>_<idx>.jpg (or .png, .mp4)
  images/, videos/                          the Media tab's photos and videos
  profile/                                  avatar_<id>.jpg and banner_<id>.jpg
One tweet, `https://x.com/<user>/status/<id>`, lands in `.flnd/downloads/twt/<id>/`.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Any, Iterator, Mapping, NamedTuple
from urllib.parse import urlsplit

from flnd.session import Session
from flnd.sources.base import FetchError, Source
from flnd.target import Target, link, name, unreadable
from shared import selector

# The user agent Twitter's API has always been sent: a desktop Chrome.
CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/128.0.0.0 Safari/537.36")
# The web client's public bearer token.
TWITTER_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
API = "https://x.com/i/api/graphql/"
# The web client's query ids, from its main.js (`queryId:"…",operationName:"…"`). They change now
# and then; a stale one is refused, and the browser takes over.
QUERIES = {
    "UserByScreenName": "AMIBMjtxEEATh4z8V9GtRg",
    "UserByRestId": "mxaYWwt52eAb6OAzYHMVzg",
    "UserOriginalsTimeline": "ZFKu1DFS63hgrkLzR3Hsdw",
    "UserMedia": "iTSlWbsmD00dAE7Sk9xvIw",
    "UserHighlightsTweets": "vukXwNm6argozncYmHqZDQ",
    "UserRepostsTimeline": "bWzOK-jDN4SYVcF6AQ22Qw",
    "UserRepliesTimeline": "fpl-c0_APDI3nZ1CeeBMbg",
    "TweetResultByRestId": "U8lc5aomF5krJzZ9vJ9wxg",
}
FEATURES = json.dumps({"responsive_web_graphql_timeline_navigation_enabled": True,
                       "responsive_web_media_download_video_enabled": True})


class Timeline(NamedTuple):
    """One timeline: its category, its query, and its tab's path under the profile, for the browser."""

    category: str
    query: str
    tab: str


TIMELINES = (
    Timeline("posts", "UserOriginalsTimeline", ""),
    Timeline("media", "UserMedia", "/media"),
    Timeline("highlights", "UserHighlightsTweets", "/highlights"),
    Timeline("reposts", "UserRepostsTimeline", "/reposts"),
    Timeline("replies", "UserRepliesTimeline", "/with_replies"),
)
# Paths on x.com that are pages of the site, not profiles.
NOT_PROFILES = {"home", "explore", "search", "i", "settings", "notifications", "messages", "hashtag"}
BANNER_SIZE = "1500x500"


class Twitter(Source):
    """Twitter / X users and single tweets, from the GraphQL API, with a browser when the API refuses."""

    code = "twt"
    name = "Twitter/X"
    domains = ("x.com", "twitter.com")
    kinds_of_target = ("user", "post")
    examples = ("twt:example_handle", "twt:@example_handle", "twt:44196397", "https://x.com/example_handle",
                "https://x.com/example_handle/status/1790000000000000000")
    needs = ("auth_token",)
    categories = ("posts", "media", "highlights", "reposts", "replies", "profile")
    single_kinds = ("post",)
    user_agent = CHROME
    # This browser never hid navigator.webdriver, so stealth stays off.
    stealth = False

    def headers(self, cookies: Mapping[str, str]) -> Mapping[str, str]:
        """Twitter API headers: the bearer token, CSRF, and the session's cookies."""
        return {
            "authorization": TWITTER_BEARER,
            "x-csrf-token": cookies.get("ct0", ""),
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session" if "auth_token" in cookies else "",
            "x-twitter-client-language": "en",
            "cookie": "; ".join([f"{k}={v}" for k, v in cookies.items()]),
        }

    def resolve(self, text: str) -> Target:
        """A screen name, a number (a user id), or one tweet: a URL with /status/<id>."""
        found = link(text, self.domains)
        if found is None:
            user = name(text)
        elif (tweet_id := status_id(found.parts)) is not None:
            return Target(self.code, "post", tweet_id)
        else:
            user = name(found.parts[0]) if found.parts and found.parts[0] not in NOT_PROFILES else None
        if not user:
            raise unreadable(self, text)
        return Target(self.code, "user", user)

    def run(self, session: Session) -> None:
        if session.target.kind == "post":
            self._save_one_tweet(session, session.target.value)
            return
        handle = session.target.value
        user = self._fetch_user(session, handle)
        if user is None:
            session.warn("the API can't find @{name}; reading the profile in the browser", name=handle)
            self._read_in_browser(session, handle, None, TIMELINES)
            return
        user_id = user["rest_id"]
        if session.wants(category="profile"):
            save_profile(session, user)
        for number, timeline in enumerate(TIMELINES):
            if session.wants(category=timeline.category) and not self._read_timeline(session, user_id, timeline):
                self._read_in_browser(session, handle, user_id, TIMELINES[number:])
                return

    def _fetch_user(self, session: Session, handle: str) -> dict | None:
        """The user, from their screen name or their number, or None when the API can't find them."""
        if handle.isdigit():
            query, variables = "UserByRestId", {"userId": handle}
        else:
            query, variables = "UserByScreenName", {"screen_name": handle}
        try:
            data = self._fetch(session, query, variables)
        except FetchError as error:
            session.warn("{query}: {error}", query=query, error=error)
            return None
        user = selector.select(data).jmespath("data.user.result").first
        return user if isinstance(user, dict) and user.get("rest_id") else None

    def _read_timeline(self, session: Session, user_id: str, timeline: Timeline) -> bool:
        """Read one timeline to its end; False when the API refused it and the browser should take over."""
        cursor, pages, seen = None, 0, set()
        while True:
            variables = {"userId": user_id, "count": 20, "includePromotedContent": False, "withVoice": True}
            if cursor:
                variables["cursor"] = cursor
            try:
                data = self._fetch(session, timeline.query, variables)
            except FetchError as error:
                # No answer, or no JSON, is not a refusal: the browser would see the same.
                if error.status is None:
                    session.warn("{category}: {error}", category=timeline.category, error=error)
                    return True
                session.warn("{category}: {error}; reading it in the browser", category=timeline.category,
                             error=error)
                return False
            tweets, cursor = read_timeline_page(data)
            pages += 1
            # Every page repeats the pinned tweet, and the last one still has a bottom cursor,
            # so the end is the first page with no tweet not seen before.
            new = [tweet for tweet in tweets if tweet_id_of(tweet) not in seen]
            seen.update(tweet_id_of(tweet) for tweet in new)
            for tweet in new:
                save_tweet(session, tweet, timeline.category, user_id)
            if not new or not cursor:
                break
        session.info("{category}: {n} pages read", category=timeline.category, n=pages)
        return True

    def _save_one_tweet(self, session: Session, tweet_id: str) -> None:
        variables = {"tweetId": tweet_id, "withCommunity": False, "includePromotedContent": False, "withVoice": False}
        try:
            data = self._fetch(session, "TweetResultByRestId", variables)
        except FetchError as error:
            session.warn("tweet {id}: {error}", id=tweet_id, error=error)
            return
        tweet = selector.select(data).jmespath("data.tweetResult.result").first
        if not isinstance(tweet, dict) or not unwrap(tweet).get("legacy"):
            session.warn("tweet {id} isn't there, or isn't visible to this login", id=tweet_id)
            return
        save_tweet(session, tweet, "posts", None, folder="")

    def _fetch(self, session: Session, query: str, variables: dict) -> Any:
        return session.fetch_json(f"{API}{QUERIES[query]}/{query}", follow_redirects=False,
                                  params={"variables": json.dumps(variables), "features": FEATURES})

    def _read_in_browser(self, session: Session, handle: str, user_id: str | None,
                         timelines: tuple[Timeline, ...]) -> None:
        """Scroll each timeline's tab to its end, reading the same GraphQL responses the API gives."""
        profile = f"https://x.com/i/user/{handle}" if handle.isdigit() else f"https://x.com/{handle}"
        found = [0]

        def on_timeline(session: Session, data: Any, response: Any) -> None:
            if session.folder is None:
                return
            tweets, _ = read_timeline_page(data)
            found[0] += len(tweets)
            for tweet in tweets:
                save_tweet(session, tweet, session.folder, user_id)

        session.on_response(lambda response: any(f"/{timeline.query}" in response.url for timeline in timelines),
                            on_timeline)
        for timeline in timelines:
            if session.wants(category=timeline.category):
                session.folder = timeline.category
                if session.goto(profile + timeline.tab):
                    session.scroll(count=lambda: found[0])
        session.folder = None
        session.info("{n} tweets read in the browser", n=found[0])


def status_id(parts: list[str]) -> str | None:
    """The tweet id in a URL's path parts (`<user>/status/<id>`, `i/web/status/<id>`), or None."""
    for part, tweet_id in zip(parts, parts[1:]):
        if part == "status" and tweet_id.isdigit():
            return tweet_id
    return None


def read_timeline_page(data: Any) -> tuple[list[dict], str | None]:
    """One timeline page's tweets, and the cursor of the next page (None at the end)."""
    instructions = selector.select(data).jmespath(
        "data.user.result.timeline.timeline.instructions || data.user.result.timeline_v2.timeline.instructions").all
    tweets, cursor = [], None
    for entry in timeline_entries(instructions):
        content = entry.get("content") or entry.get("item") or {}
        if content.get("cursorType") == "Bottom":
            cursor = content.get("value")
        items = [item.get("item", item) for item in content.get("items", [])] or [content]
        for item in items:
            tweet = selector.select(item).jmespath("itemContent.tweet_results.result").first
            if isinstance(tweet, dict):
                tweets.append(tweet)
    return tweets, cursor


def timeline_entries(instructions: list[Any]) -> Iterator[dict]:
    """Every entry of a page: added ones, the pinned one, and items added to a grid (the Media tab's later pages)."""
    for instruction in instructions:
        if isinstance(instruction, dict):
            yield from instruction.get("entries", [])
            yield from instruction.get("moduleItems", [])
            if "entry" in instruction:
                yield instruction["entry"]


def unwrap(tweet: dict) -> dict:
    """The tweet itself, out of the wrapper a tweet with limited visibility comes in."""
    inner = tweet.get("tweet")
    return inner if isinstance(inner, dict) else tweet


def tweet_id_of(tweet: dict) -> str:
    """A tweet's id, inside its visibility wrapper or not."""
    tweet = unwrap(tweet)
    return str(tweet.get("rest_id") or selector.select(tweet).jmespath("legacy.id_str").first or "")


def save_tweet(session: Session, tweet: dict, category: str, user_id: str | None, *,
               folder: str | None = None) -> None:
    """Save a tweet's photos and videos into its category's folder, when the category takes it.

    Reposts take the retweeted tweet's files; replies take only the user's own replies. Media
    files go to images/ or videos/, and everything else to its category's folder.
    """
    tweet = unwrap(tweet)
    legacy = tweet.get("legacy") or {}
    retweeted = selector.select(legacy).jmespath("retweeted_status_result.result").first
    if category == "reposts":
        if not isinstance(retweeted, dict):
            return
        tweet = unwrap(retweeted)
        legacy = tweet.get("legacy") or {}
    elif retweeted is not None:
        return
    if category == "replies" and (legacy.get("user_id_str") != user_id or not legacy.get("in_reply_to_status_id_str")):
        return
    tweet_id, day = tweet_id_of(tweet), posted_on(legacy.get("created_at"))
    for index, media in enumerate(selector.select(legacy).jmespath("extended_entities.media").all):
        file = best_file(media)
        if file is None:
            continue
        url, extension = file
        if folder is None:
            target_folder = ("videos" if extension == "mp4" else "images") if category == "media" else category
        else:
            target_folder = folder
        file_name = f"{day}_{tweet_id}_{index}.{extension}" if day else f"{tweet_id}_{index}.{extension}"
        session.save(url, file_name, category=category, date=day, folder=target_folder, key=f"{tweet_id}_{index}")


def best_file(media: dict) -> tuple[str, str] | None:
    """A photo at full size, or a video's highest-bitrate MP4, as `(url, extension)`."""
    if media.get("type") == "photo" and media.get("media_url_https"):
        url = media["media_url_https"]
        return f"{url}?name=orig", PurePosixPath(urlsplit(url).path).suffix.lstrip(".") or "jpg"
    variants = selector.select(media).jmespath("video_info.variants").all
    mp4s = [variant for variant in variants if variant.get("content_type") == "video/mp4"]
    if not mp4s:
        return None
    return max(mp4s, key=lambda variant: variant.get("bitrate", 0))["url"], "mp4"


def save_profile(session: Session, user: dict) -> None:
    """Save the user's avatar at its original size, and their banner at its largest."""
    avatar = selector.select(user).jmespath("avatar.image_url || legacy.profile_image_url_https").first
    banner = selector.select(user).jmespath("banner.image_url || legacy.profile_banner_url").first
    if avatar:
        # "_normal" is the 48px version; without it the URL gives the picture as uploaded.
        original = avatar.replace("_normal.", ".")
        picture_id = PurePosixPath(urlsplit(original).path).parent.name
        session.save(original, f"avatar_{picture_id}{PurePosixPath(original).suffix or '.jpg'}",
                     category="profile", folder="profile")
    if banner:
        session.save(f"{banner}/{BANNER_SIZE}", f"banner_{banner.rstrip('/').rsplit('/', 1)[-1]}.jpg",
                     category="profile", folder="profile")


def posted_on(created_at: str | None) -> date | None:
    """A tweet's day, from Twitter's `Tue Nov 10 14:22:30 +0000 2026`."""
    if not created_at:
        return None
    return datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y").date()
