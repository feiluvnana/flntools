"""Twitter / X: a user's posts, media, highlights, reposts, replies and profile pictures.

The user route reads profile metadata once from GraphQL and hands it to each category.
Every timeline category streams posts while scrolling its tab to the end in a browser.
A post link reads one tweet from GraphQL and yields its media.

    flnd download twt:example_handle --from posts,profile
    flnd download https://x.com/example_handle/status/1790000000000000000

Under .flnd/media/twt/<username>/:
  posts/, highlights/, reposts/, replies/   <date>_<tweet_id>_<n>.<ext>
  images/, videos/                          Media tab photos and videos
  profile/                                  avatar_<id>.jpg and banner_<id>.jpg
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Iterator, Mapping, NamedTuple
from urllib.parse import urlsplit

import msgspec

from flnd.source import (
    FetchError, File, LoginError, Post, Site, Source, caught_up, day, target,
)
from flnd.sources.common.feed import warm_tabs, watch_responses
from shared import http, selector, text

if TYPE_CHECKING:
    import httpx

# The user agent Twitter's API has always been sent: a desktop Chrome.
CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/128.0.0.0 Safari/537.36")
WALLS = ("/i/flow/login", "/login", "/account/access")
# The web client's public bearer token.
TWITTER_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
API = "https://x.com/i/api/graphql/"
# The web client's query ids, from its main.js (`queryId:"…",operationName:"…"`). They change now
# and then; a stale one is refused, and the browser takes over.
QUERIES = {
    "UserByScreenName": "AMIBMjtxEEATh4z8V9GtRg",
    # gallery-dl's id, unverified here; a stale id is refreshed from x.com's script like any other.
    "SearchTimeline": "nK1dw4oV3k4w5TdtcAdSww",
    "UserByRestId": "mxaYWwt52eAb6OAzYHMVzg",
    "UserOriginalsTimeline": "ZFKu1DFS63hgrkLzR3Hsdw",
    "UserMedia": "iTSlWbsmD00dAE7Sk9xvIw",
    "UserHighlightsTweets": "vukXwNm6argozncYmHqZDQ",
    "UserRepostsTimeline": "bWzOK-jDN4SYVcF6AQ22Qw",
    "UserRepliesTimeline": "fpl-c0_APDI3nZ1CeeBMbg",
    "TweetResultByRestId": "U8lc5aomF5krJzZ9vJ9wxg",
}
# The flags sent before the web app's own are known: a refresh replaces them with each query's
# `featureSwitches` and the values x.com's page gives them.
FEATURES = {"responsive_web_graphql_timeline_navigation_enabled": True,
            "responsive_web_media_download_video_enabled": True}
HOME = "https://x.com/"
# The web app's main script, which x.com's page loads: it holds every operation's query id.
MAIN_SCRIPT = re.compile(
    r"https://abs\.twimg\.com/responsive-web/client-web(?:-legacy)?/main\.[\w.-]+\.js"
)
# One operation in that script, as the web app's bundler writes it:
# Operation in script: queryId:"…",operationName:"UserMedia",metadata:{featureSwitches:[...]}.
OPERATION = re.compile(
    r'queryId:"(?P<id>[\w-]+)",operationName:"(?P<name>\w+)",operationType:"\w+",'
    r"metadata:\{featureSwitches:\[(?P<features>[^\]]*)\]"
)
# A feature switch's value in the page's `__INITIAL_STATE__` (`featureSwitch.defaultConfig` and the
# user's own config after it, which wins): `"rweb_video_screen_enabled":{"value":false}`.
SWITCH = re.compile(r'"(?P<name>[a-z0-9_]+)":\{"value":(?P<value>true|false)\b')
# What a stale query id is answered with: a 404 for an id the API no longer knows, a 400 for a
# query whose variables or features it no longer takes.
STALE = frozenset({400, 404})
# Where the refreshed table is kept in the source's `state`.
STATE_KEY = "graphql.queries"
# Consecutive tweets older than `since` before stopping scroll early (covers pinned tweets).
OLD_STREAK = 5
# A tweet's `created_at`, as Twitter writes it: `Mon Nov 09 10:00:00 +0000 2026`, always UTC.
CREATED_AT = "%a %b %d %H:%M:%S %z %Y"
# The largest banner size the CDN serves, as the `/<size>` suffix of a banner's URL.
BANNER_SIZE = "1500x500"


class Timeline(NamedTuple):
    """One timeline: category, GraphQL query, and tab path under profile for browser."""

    # Category posts are filed under: "posts", "media", "highlights", "reposts" or "replies".
    category: str
    # The GraphQL operation whose responses carry the timeline, as a key of `QUERIES`.
    query: str
    # The tab's path under the profile's URL, which the browser opens; "" is the profile itself.
    tab: str


TIMELINES = {timeline.category: timeline for timeline in (
    Timeline("posts", "UserOriginalsTimeline", ""),
    Timeline("media", "UserMedia", "/media"),
    Timeline("highlights", "UserHighlightsTweets", "/highlights"),
    Timeline("reposts", "UserRepostsTimeline", "/reposts"),
    Timeline("replies", "UserRepliesTimeline", "/with_replies"),
)}


@dataclass
class Account:
    """What the `user` route gives every category: who is read, and their timelines so far."""

    # The screen name or the number the target was given.
    handle: str
    # The API's user object; None when the API can't find them, and the browser reads everything.
    user: dict | None

    @property
    def id(self) -> str | None:
        """The user's id, or None when the API doesn't know them."""
        return self.user["rest_id"] if self.user else None

# A card's image fields, best first: the full-size picture, then the smaller previews.
CARD_IMAGES = (
    "photo_image_full_size_original", "summary_photo_image_original", "player_image_original",
    "thumbnail_image_original", "photo_image_full_size_large", "summary_photo_image_large",
)
# The sizes X's media server gives a photo in, best first: `orig` is the upload itself.
PHOTO_SIZES = ("orig", "4096x4096", "large", "medium")

class Twitter(Source):
    """Twitter / X users and single tweets from GraphQL API, with browser fallback."""

    code = "twt"
    name = "Twitter/X"
    domains = ("x.com", "twitter.com")
    # Paths on the site that are pages of it, not profiles. x.com/i covers x.com/i/* too,
    # except the tweet links post reads (more literal segments win).
    unsupported = {
        f"{host}/{path}": "that's not a profile"
        for host in ("x.com", "twitter.com")
        for path in (
            "home", "explore", "search", "settings", "notifications", "messages",
            "hashtag", "i", "intent", "compose", "login",
        )
    }
    needs = ("auth_token",)
    # X's `x-rate-limit-*` headers stop flnd exactly at the limit, so the floor only spaces a burst.
    pace = 0.2
    user_agent = CHROME
    browser = True
    # This browser never hid navigator.webdriver, so stealth stays off.
    stealth = False
    # A post is one photo or video of a tweet, so a file is <date>_<tweet id>_<n> whatever the
    # tweet's media count; part is the category folder, images/videos for Media, "" for a tweet.
    folder = "{part}"
    fields = ("part", "n")
    filename = "{date}_{id}_{n}"
    key = "{id}_{n}"

    def __init__(self) -> None:
        # The GraphQL query table for this run; one source instance serves the whole run.
        self.graph = Graph()

    def prepare(self, request: httpx.Request, cookies: Mapping[str, str], /) -> httpx.Request:
        """Twitter's API headers on every request to x.com's API, with current CSRF token."""
        # The page and script (Graph.refresh reads them) return 401 to the API's bearer token.
        if not request.url.path.startswith("/i/api/"):
            return request
        request.headers["authorization"] = TWITTER_BEARER
        # The API checks that this equals the `ct0` cookie it also receives (double-submit CSRF).
        request.headers["x-csrf-token"] = cookies.get("ct0", "")
        request.headers["x-twitter-active-user"] = "yes"
        request.headers["x-twitter-client-language"] = "en"
        if "auth_token" in cookies:
            request.headers["x-twitter-auth-type"] = "OAuth2Session"
        return request

    # `post` comes before `user`, so x.com/status/<id> is never the user "status".
    @target(r"x.com/*/status/{tweet_id:\d+}", r"twitter.com/*/status/{tweet_id:\d+}",
            r"x.com/status/{tweet_id:\d+}", r"twitter.com/status/{tweet_id:\d+}",
            r"x.com/i/web/status/{tweet_id:\d+}", r"twitter.com/i/web/status/{tweet_id:\d+}")
    def post(self, site: Site, tweet_id: str) -> Iterator[Post]:
        """One tweet, by its id: its media, in the target's own folder."""
        variables = {
            "tweetId": tweet_id, "withCommunity": False,
            "includePromotedContent": False, "withVoice": False,
        }
        try:
            data = self.graph.get(site, "TweetResultByRestId", variables)
        except (FetchError, http.Error) as error:
            site.warn("tweet {id}: {error}", id=tweet_id, error=error)
            return
        tweet = selector.parse(data).jmespath("data.tweetResult.result").first
        if not isinstance(tweet, dict) or not unwrap(tweet).get("legacy"):
            site.warn("tweet {id} isn't there, or isn't visible to this login", id=tweet_id)
            return
        yield from tweet_posts(tweet, "posts", None, part="")

    @target("{user}", "x.com/{user}", "twitter.com/{user}",
            r"x.com/i/user/{user:\d+}", r"twitter.com/i/user/{user:\d+}")
    def user(self, site: Site, user: str) -> Account:
        """A profile by screen name, or numeric user id: read once for every category."""
        # A number is a user id, so it is asked by `UserByRestId`; anything else is a screen name.
        if user.isdigit():
            query, variables = "UserByRestId", {"userId": user}
        else:
            query, variables = "UserByScreenName", {"screen_name": user}
        found = None
        try:
            found = selector.parse(
                self.graph.get(site, query, variables)
            ).jmespath("data.user.result").first
        except (FetchError, http.Error) as error:
            site.warn("{query}: {error}", query=query, error=error)
        if isinstance(found, dict) and found.get("rest_id"):
            return Account(user, found)
        site.warn("the API can't find @{name}; reading the profile in the browser", name=user)
        return Account(user, None)

    # Newest first: a profile's tweets, its Media tab and its replies come by date, the pinned
    # tweet apart (the engine's 5-in-a-row rule covers it). Highlights and reposts come in the
    # order they were added, which is not the tweets' dates.
    @user.category(newest_first=True)
    def posts(self, site: Site, account: Account) -> Iterator[Post]:
        """The user's own tweets (retweets left out)."""
        yield from self.read_browser(site, account, TIMELINES["posts"])

    @user.category(newest_first=True)
    def media(self, site: Site, account: Account) -> Iterator[Post]:
        """The Media tab: every photo and video, filed in images/ or videos/."""
        yield from self.read_browser(site, account, TIMELINES["media"])

    @user.category
    def highlights(self, site: Site, account: Account) -> Iterator[Post]:
        """The tweets the user highlighted."""
        yield from self.read_browser(site, account, TIMELINES["highlights"])

    @user.category
    def reposts(self, site: Site, account: Account) -> Iterator[Post]:
        """The media of the tweets the user retweeted, dated by the original tweet."""
        yield from self.read_browser(site, account, TIMELINES["reposts"])

    @user.category(newest_first=True)
    def replies(self, site: Site, account: Account) -> Iterator[Post]:
        """The user's own replies; the tweets they answered are left out."""
        yield from self.read_browser(site, account, TIMELINES["replies"])

    # The pictures are named after the picture's own id and keyed by their path, as always.
    @user.category(folder="profile", key="")
    def profile(self, site: Site, account: Account) -> Iterator[Post]:
        """The avatar at its original size, and the banner at its largest."""
        if account.user is None:
            return
        user = account.user
        avatar = selector.parse(user).jmespath(
            "avatar.image_url || legacy.profile_image_url_https"
        ).first
        banner = selector.parse(user).jmespath(
            "banner.image_url || legacy.profile_banner_url"
        ).first
        # The avatar comes from either place the API has put it; the banner the same way.
        if avatar:
            # "_normal" is the 48px version; without it the URL gives the picture as uploaded.
            original = avatar.replace("_normal.", ".")
            picture_id = PurePosixPath(urlsplit(original).path).parent.name
            name = f"avatar_{picture_id}{PurePosixPath(original).suffix or '.jpg'}"
            yield Post(user["rest_id"], [File(original, name=name)])
        if banner:
            banner_name = f"banner_{banner.rstrip('/').rsplit('/', 1)[-1]}.jpg"
            yield Post(user["rest_id"], [File(f"{banner}/{BANNER_SIZE}", name=banner_name)])

    def read_browser(self, site: Site, account: Account, timeline: Timeline) -> Iterator[Post]:
        """Scroll one timeline's tab to its end, streaming its posts."""
        # A number is opened by its id under `/i/user/`; a screen name under its own path.
        profile = (
            f"https://x.com/i/user/{account.handle}"
            if account.handle.isdigit()
            else f"https://x.com/{account.handle}"
        )
        for tweet in self._scroll_timeline(site, profile, timeline):
            yield from tweet_posts(tweet, timeline.category, account.id)

    def _scroll_timeline(self, site: Site, profile: str, timeline: Timeline) -> Iterator[dict]:
        """Scroll one timeline tab until no new tweets arrive, `OLD_STREAK`, or `caught_up`."""
        since = site.time.since if timeline.category in ("posts", "media", "replies") else None
        seen: set[str] = set()
        old = 0
        with watch_responses(site, f"/{timeline.query}", scroll=True) as responses:
            if not site.goto(profile + timeline.tab):
                return
            if urlsplit(site.page.url).path.startswith(WALLS):
                raise LoginError(
                    "twt: X asked for a login or locked this account; "
                    "check it in a browser or run flnd auth login twt"
                )
            names = list(TIMELINES)
            later = names[names.index(timeline.category) + 1:]
            warm_tabs(site, [profile + TIMELINES[name].tab for name in later])
            for data in responses:
                tweets, _ = read_timeline_page(data)
                new = [tweet for tweet in tweets if tweet_id_of(tweet) not in seen]
                if not new:
                    break
                seen.update(tweet_id_of(tweet) for tweet in new)
                yield from new
                old = older_streak(new, since, old)
                if old >= OLD_STREAK or _hit_saved(new, timeline.category):
                    break


class Graph:
    """The GraphQL queries one run sends: their ids and feature flags, refreshed when stale."""

    def __init__(self) -> None:
        # Each operation's id and flags, by its name; None until the first query reads `state`.
        self.queries: dict[str, dict[str, Any]] | None = None
        self.refreshed = False

    def get(self, site: Site, query: str, variables: dict) -> Any:
        """One GraphQL query's JSON; a stale query id refreshed once and the query sent again."""
        try:
            return self._get(site, query, variables)
        # Only a stale id (400 or 404) is worth a refresh; any other refusal is raised as it is.
        except FetchError as error:
            if error.status not in STALE or self.refreshed or not self.refresh(site):
                raise
        return self._get(site, query, variables)

    def refresh(self, site: Site) -> bool:
        """Read current query ids and flags from x.com's page and main script, once a run."""
        # Once a run: two requests, so a second stale answer does not repeat them.
        self.refreshed = True
        # Not fatal: without refresh the query fails with its error, and browser takes over.
        try:
            home = site.fetch(HOME).text
            found = MAIN_SCRIPT.search(home)
            if found is None:
                raise FetchError("x.com's page names no main script")
            script = site.fetch(found[0]).text
        except (FetchError, http.Error) as error:
            site.warn(
                "the API's query ids look stale, and x.com's script couldn't be read: {error}",
                error=error,
            )
            return False
        switches = {match["name"]: match["value"] == "true" for match in SWITCH.finditer(home)}
        queries = {}
        for match in OPERATION.finditer(script):
            if match["name"] in QUERIES:
                names = re.findall(r'"([\w]+)"', match["features"])
                queries[match["name"]] = {
                    "id": match["id"],
                    "features": {name: switches.get(name, False) for name in names},
                }
        if not queries:
            site.warn("the API's query ids look stale, and x.com's script lists none of them")
            return False
        self._table(site).update(queries)
        site.state[STATE_KEY] = dict(self._table(site))
        n = text.count(len(queries), "query id")
        site.info("refreshed {n} from x.com's script", n=n)
        return True

    def _get(self, site: Site, query: str, variables: dict) -> Any:
        """One GraphQL query's JSON with the table's id and flags for it."""
        entry = self._table(site).get(query) or {}
        # The refreshed flags when the table has them, else the built-in ones.
        features = entry.get("features") or FEATURES
        endpoint = f"{API}{entry.get('id') or QUERIES[query]}/{query}"
        params = {
            "variables": msgspec.json.encode(variables).decode(),
            "features": msgspec.json.encode(features).decode(),
        }
        return site.fetch(endpoint, follow_redirects=False, params=params).json

    def _table(self, site: Site) -> dict[str, dict[str, Any]]:
        """The query table: what `state` kept from an earlier refresh, read on first use."""
        if self.queries is None:
            kept = site.state.get(STATE_KEY)
            self.queries = (
                {name: dict(entry) for name, entry in kept.items()
                 if isinstance(entry, dict) and name in QUERIES}
                if isinstance(kept, dict) else {}
            )
        return self.queries


def read_timeline_page(data: Any) -> tuple[list[dict], str | None]:
    """One timeline page's tweets, and the cursor of the next page (None at the end)."""
    # The timeline sits under user in one response shape and under timeline_v2 in the other; a
    # search's under search_by_raw_query.
    instructions = selector.parse(data).jmespath(
        "data.user.result.timeline.timeline.instructions"
        " || data.user.result.timeline_v2.timeline.instructions"
        " || data.search_by_raw_query.search_timeline.timeline.instructions"
    ).all
    tweets, cursor = [], None
    for entry in timeline_entries(instructions):
        content = entry.get("content") or entry.get("item") or {}
        # The page's `Bottom` cursor is the request for the next page; the `Top` one is not used.
        if content.get("cursorType") == "Bottom":
            cursor = content.get("value")
        items = [item.get("item", item) for item in content.get("items", [])] or [content]
        for item in items:
            tweet = selector.parse(item).jmespath("itemContent.tweet_results.result").first
            if isinstance(tweet, dict):
                tweets.append(tweet)
    return tweets, cursor


def timeline_entries(instructions: list[Any]) -> Iterator[dict]:
    """Every entry of a page: added ones, the pinned one, and items added to a grid."""
    for instruction in instructions:
        if isinstance(instruction, dict):
            yield from instruction.get("entries", [])
            # The Media tab's later pages come as `moduleItems`, not `entries`.
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
    return str(tweet.get("rest_id") or selector.parse(tweet).jmespath("legacy.id_str").first or "")


def tweet_date(tweet: dict) -> datetime | None:
    """A tweet's `created_at` timestamp as a datetime, or None."""
    created_at = (unwrap(tweet).get("legacy") or {}).get("created_at")
    return datetime.strptime(created_at, CREATED_AT) if created_at else None


def older_streak(tweets: list[dict], since: date | None, streak: int) -> int:
    """Count consecutive tweets older than `since`, carrying `streak` forward."""
    if since is None:
        return streak
    for tweet in tweets:
        posted = day(tweet_date(tweet))
        if posted is None:
            continue
        streak = streak + 1 if posted < since else 0
    return streak


def _hit_saved(tweets: list[dict], category: str) -> bool:
    """Whether a `newest_first` timeline batch hits the 5-saved-post early-stop streak."""
    if category not in ("posts", "media", "replies"):
        return False
    for tweet in tweets:
        if not list(tweet_posts(tweet, category, None)):
            continue
        tid = tweet_id_of(tweet)
        if tid and caught_up(tid, posted=tweet_date(tweet)):
            return True
    return False


def tweet_posts(
    tweet: dict, category: str, user_id: str | None, *, part: str | None = None,
) -> Iterator[Post]:
    """A tweet's photos and videos as posts, one each, when the category takes the tweet.

    Reposts take the retweeted tweet's files; replies take only the user's own replies; the other
    categories leave retweets out. Quoted tweet media and card images are included.
    """
    tweet = unwrap(tweet)
    legacy = tweet.get("legacy") or {}
    retweeted = selector.parse(legacy).jmespath("retweeted_status_result.result").first
    # A repost's files are the retweeted tweet's, so the reposts category reads that tweet instead.
    if category == "reposts":
        if not isinstance(retweeted, dict):
            return
        tweet = unwrap(retweeted)
        legacy = tweet.get("legacy") or {}
    # Retweets are left out of every other category: their media is filed under reposts.
    elif retweeted is not None:
        return
    # A reply is kept only when it is the user's own and answers another tweet.
    if category == "replies" and (
        user_id is None
        or legacy.get("user_id_str") != user_id
        or not legacy.get("in_reply_to_status_id_str")
    ):
        return
    tweet_id, created_at = tweet_id_of(tweet), legacy.get("created_at")
    posted = datetime.strptime(created_at, CREATED_AT) if created_at else None
    quoted = selector.parse(tweet).jmespath("quoted_status_result.result").first
    quoted_media = (
        selector.parse(unwrap(quoted).get("legacy") or {}).jmespath("extended_entities.media").all
        if isinstance(quoted, dict) else []
    )
    found_media = [(index, media) for index, media in
                   enumerate(selector.parse(legacy).jmespath("extended_entities.media").all)]
    found_media += [(f"q{index}", media) for index, media in enumerate(quoted_media)]
    for index, media in found_media:
        found = best_file(media)
        if found is None:
            continue
        url, extension = found
        if part is None:
            folder = (
                ("videos" if extension == "mp4" else "images")
                if category == "media" else category
            )
        else:
            folder = part
        yield Post(
            tweet_id, [media_file(url, extension, tweet_id, index, posted)],
            date=posted, part=folder, n=index,
        )
    card = card_image(tweet)
    if card is not None:
        folder = part if part is not None else ("images" if category == "media" else category)
        yield Post(
            tweet_id, [media_file(card, "jpg", tweet_id, "card", posted)],
            date=posted, part=folder, n="card",
        )


def card_image(tweet: dict) -> str | None:
    """A tweet's card image, at its largest; None when it has no card or no image."""
    values = selector.parse(tweet).jmespath("card.legacy.binding_values").all
    images = {
        entry.get("key"): selector.parse(entry).jmespath("value.image_value.url").first
        for entry in values if isinstance(entry, dict)
    }
    return next((images[key] for key in CARD_IMAGES if images.get(key)), None)


def media_file(
    url: str | tuple[str, ...], extension: str, tweet_id: str, index: int | str,
    posted: datetime | None,
) -> File:
    """A media file, named from its URL's extension; pinned when the URL gives none to use."""
    suffix = PurePosixPath(urlsplit(url if isinstance(url, str) else url[0]).path).suffix
    if suffix and suffix == suffix.lower():
        return File(url)
    stem = "_".join(str(part) for part in (day(posted), tweet_id, index) if part is not None)
    return File(url, name=f"{stem}.{extension}")


def best_file(media: dict) -> tuple[str | tuple[str, ...], str] | None:
    """A photo with mirror URLs, or a video's highest-bitrate MP4, as (url, extension)."""
    if media.get("type") == "photo" and media.get("media_url_https"):
        url = media["media_url_https"]
        # `name=orig` asks the media server for the full-size picture, not its thumbnail size; the
        # smaller sizes are mirrors for when it refuses one, tried in this order.
        sizes = tuple(f"{url}?name={size}" for size in PHOTO_SIZES)
        return sizes, PurePosixPath(urlsplit(url).path).suffix.lstrip(".") or "jpg"
    variants = selector.parse(media).jmespath("video_info.variants").all
    # Only MP4 variants are kept: streaming playlists aren't files the engine can save as MP4.
    mp4s = [variant for variant in variants if variant.get("content_type") == "video/mp4"]
    if not mp4s:
        return None
    return max(mp4s, key=lambda variant: variant.get("bitrate", 0))["url"], "mp4"
