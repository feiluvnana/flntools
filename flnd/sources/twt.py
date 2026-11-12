"""Twitter / X: a user's posts, media, highlights, reposts, replies and profile pictures, or one tweet.

How it works: the `user` route reads the profile once (`UserByScreenName`, or `UserByRestId` for
a number) and gives it to each category. A timeline category pages the GraphQL API to its end
(`pages`, 50 tweets a page) and yields one `Post` per photo or video of each tweet. When the API
refuses a timeline (an old query id, say), the same category scrolls that tab in a browser and
reads the same GraphQL responses (`watch_responses`), and so do the categories after it. A
`post` link reads one tweet (`TweetResultByRestId`) and yields its media itself.

    flnd download twt:example_handle --from posts,profile
    flnd download https://x.com/example_handle/status/1790000000000000000

Query ids and feature flags: the web client's GraphQL ids change now and then, and each query
wants the feature flags the web app sends for it. When a query is refused as stale (a 400 or 404),
`Graph` reads x.com's page and its main script once a run: the script lists each operation's
`queryId` and the `featureSwitches` it takes, and the page the switches' values. The query is then
sent again once with the current id and the web app's own flags, and the table is kept in the
source's `state`, so the next run starts with it. Only when that fails too does the browser take over.

What the site does that the code works around:
  - the API wants the web client's public bearer token, the login's `ct0` cookie echoed in
    `x-csrf-token`, and a Chrome user agent, sent to x.com only (`prepare`); the media servers
    (`pbs.twimg.com`, `video.twimg.com`) get none of it, and no login cookie;
  - every page repeats the pinned tweet, and the last page still has a bottom cursor, so a
    timeline ends at the first page with no tweet not seen before;
  - a tweet with limited visibility comes wrapped in another object (`unwrap`);
  - the Media tab's later pages come as `moduleItems`, not `entries` (`timeline_entries`);
  - photos are saved at full resolution (`name=orig`), and videos as the highest-bitrate MP4;
  - a tweet's file found on two timelines (most posts are on the Media tab too) is saved once,
    because both are named and keyed the same: `<id>_<n>`, `n` counting the tweet's media from 0.

Reposts are the media of the tweets the user retweeted; replies, the user's own replies.

Folder layout, under `.flnd/downloads/twt/<username>/`:
  posts/, highlights/, reposts/, replies/   <YYYY-MM-DD>_<tweet_id>_<n>.jpg (or .png, .mp4)
  images/, videos/                          the Media tab's photos and videos
  profile/                                  avatar_<id>.jpg and banner_<id>.jpg
One tweet, `https://x.com/<user>/status/<id>`, lands in `.flnd/downloads/twt/<id>/`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Generator, Iterator, Mapping, NamedTuple
from urllib.parse import urlsplit

from flnd.source import FetchError, File, Post, Site, Source, day, pages, target
from flnd.sources.common.watch import watch_responses
from shared import fmt, http, selector

if TYPE_CHECKING:
    import httpx

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
# The flags sent before the web app's own are known: a refresh replaces them with each query's
# `featureSwitches` and the values x.com's page gives them.
FEATURES = {"responsive_web_graphql_timeline_navigation_enabled": True,
            "responsive_web_media_download_video_enabled": True}
HOME = "https://x.com/"
# The web app's main script, which x.com's page loads: it holds every operation's query id.
MAIN_SCRIPT = re.compile(r"https://abs\.twimg\.com/responsive-web/client-web(?:-legacy)?/main\.[\w.-]+\.js")
# One operation in that script, as the web app's bundler writes it (to be confirmed by a live run):
# `queryId:"…",operationName:"UserMedia",operationType:"query",metadata:{featureSwitches:["a","b"],…}`.
OPERATION = re.compile(r'queryId:"(?P<id>[\w-]+)",operationName:"(?P<name>\w+)",operationType:"\w+",'
                       r"metadata:\{featureSwitches:\[(?P<features>[^\]]*)\]")
# A feature switch's value in the page's `__INITIAL_STATE__` (`featureSwitch.defaultConfig` and the
# user's own config after it, which wins): `"rweb_video_screen_enabled":{"value":false}`.
SWITCH = re.compile(r'"(?P<name>[a-z0-9_]+)":\{"value":(?P<value>true|false)\b')
# What a stale query id is answered with: a 404 for an id the API no longer knows, a 400 for a
# query whose variables or features it no longer takes.
STALE = frozenset({400, 404})
# Where the refreshed table is kept in the source's `state`.
STATE_KEY = "graphql.queries"
# Tweets asked for per page: what the web app itself asks for, 2.5 times fewer requests than 20.
PAGE_SIZE = 50
# A tweet's `created_at`, as Twitter writes it: `Mon Nov 09 10:00:00 +0000 2026`, always UTC.
CREATED_AT = "%a %b %d %H:%M:%S %z %Y"
# The largest banner size the CDN serves, as the `/<size>` suffix of a banner's URL.
BANNER_SIZE = "1500x500"


class Timeline(NamedTuple):
    """One timeline: its category, its GraphQL query, and its tab's path under the profile, for the browser."""

    # The category the posts are filed under: "posts", "media", "highlights", "reposts" or "replies".
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
    """What the `user` route gives every category: who is read, and how their timelines are read so far."""

    # The screen name or the number the target was given.
    handle: str
    # The API's user object; None when the API can't find them, and the browser reads everything.
    user: dict | None
    # Set once a timeline was refused, so the categories after it go straight to the browser.
    browser: bool = False

    @property
    def id(self) -> str | None:
        """The user's id, or None when the API doesn't know them."""
        return self.user["rest_id"] if self.user else None


class Twitter(Source):
    """Twitter / X users and single tweets, from the GraphQL API, with a browser when the API refuses."""

    code = "twt"
    name = "Twitter/X"
    domains = ("x.com", "twitter.com")
    # Paths on the site that are pages of it, not profiles. `x.com/i` also covers `x.com/i/*`, except the tweet links `post` reads (more literal segments win).
    unsupported = {
        "x.com/home": "that's not a profile", "twitter.com/home": "that's not a profile",
        "x.com/explore": "that's not a profile", "twitter.com/explore": "that's not a profile",
        "x.com/search": "that's not a profile", "twitter.com/search": "that's not a profile",
        "x.com/settings": "that's not a profile", "twitter.com/settings": "that's not a profile",
        "x.com/notifications": "that's not a profile", "twitter.com/notifications": "that's not a profile",
        "x.com/messages": "that's not a profile", "twitter.com/messages": "that's not a profile",
        "x.com/hashtag": "that's not a profile", "twitter.com/hashtag": "that's not a profile",
        "x.com/i": "that's not a profile", "twitter.com/i": "that's not a profile",
    }
    needs = ("auth_token",)
    user_agent = CHROME
    # This browser never hid navigator.webdriver, so stealth stays off.
    stealth = False
    # A post is one photo or video of a tweet, so a file is `<date>_<tweet id>_<n>` whatever the tweet's
    # count of media; `part` is the folder: the category, `images`/`videos` for the Media tab, "" for one tweet.
    folder = "{part}"
    filename = "{date}_{id}_{n}"
    key = "{id}_{n}"

    def __init__(self) -> None:
        # The GraphQL query table for this run; one source instance serves the whole run.
        self.graph = Graph()

    def prepare(self, request: httpx.Request, cookies: Mapping[str, str], /) -> httpx.Request:
        """Twitter's API headers on every request to x.com's API, with the CSRF token the jar holds now.

        Args:
            request: the request about to be sent; only x.com's and twitter.com's reach here.
            cookies: the login's cookies as they are now. x.com refreshes `ct0` with `Set-Cookie`,
                and the API answers 403 to an old one, so the token is read here each time.

        Returns:
            The same request, with the headers set. No `cookie` header: the client's jar sends
            x.com's own cookies, and a media server never sees the login.
        """
        # The page and its script (`Graph.refresh` reads them) are answered 401 to the API's bearer token.
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
        """One tweet, by its id: `Target("twt", "post", "1790000000000000000")`; its media, in the target's own folder.

        Args:
            site: the target's site, which the tweet is read through.
            tweet_id: the tweet's numeric id, from the target or the URL.
        """
        variables = {"tweetId": tweet_id, "withCommunity": False, "includePromotedContent": False, "withVoice": False}
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

    @target("{user}", "x.com/{user}", "twitter.com/{user}")
    def user(self, site: Site, user: str) -> Account:
        """A profile, by its screen name, or a number, which is a user id and never a tweet: read once, for every category.

        Args:
            site: the target's site, which the user is read through.
            user: the screen name without `@`, or the user's numeric id.
        """
        # A number is a user id, so it is asked by `UserByRestId`; anything else is a screen name.
        if user.isdigit():
            query, variables = "UserByRestId", {"userId": user}
        else:
            query, variables = "UserByScreenName", {"screen_name": user}
        found = None
        try:
            found = selector.parse(self.graph.get(site, query, variables)).jmespath("data.user.result").first
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
        """The user's own tweets (retweets left out).

        Args:
            site: the target's site.
            account: the `Account` the route built, read once for every category.
        """
        yield from self.timeline(site, account, TIMELINES["posts"])

    @user.category(newest_first=True)
    def media(self, site: Site, account: Account) -> Iterator[Post]:
        """The Media tab: every photo and video, filed in `images/` or `videos/`.

        Args:
            site: the target's site.
            account: the `Account` the route built, read once for every category.
        """
        yield from self.timeline(site, account, TIMELINES["media"])

    @user.category
    def highlights(self, site: Site, account: Account) -> Iterator[Post]:
        """The tweets the user highlighted.

        Args:
            site: the target's site.
            account: the `Account` the route built, read once for every category.
        """
        yield from self.timeline(site, account, TIMELINES["highlights"])

    @user.category
    def reposts(self, site: Site, account: Account) -> Iterator[Post]:
        """The media of the tweets the user retweeted, dated by the original tweet.

        Args:
            site: the target's site.
            account: the `Account` the route built, read once for every category.
        """
        yield from self.timeline(site, account, TIMELINES["reposts"])

    @user.category(newest_first=True)
    def replies(self, site: Site, account: Account) -> Iterator[Post]:
        """The user's own replies; the tweets they answered are left out.

        Args:
            site: the target's site.
            account: the `Account` the route built, read once for every category.
        """
        yield from self.timeline(site, account, TIMELINES["replies"])

    # The pictures are named after the picture's own id and keyed by their path, as always.
    @user.category(folder="profile", key="")
    def profile(self, site: Site, account: Account) -> Iterator[Post]:
        """The avatar at its original size, and the banner at its largest.

        Args:
            site: the target's site.
            account: the `Account` the route built; with no user (the API couldn't find them) nothing is saved.
        """
        if account.user is None:
            return
        user = account.user
        avatar = selector.parse(user).jmespath("avatar.image_url || legacy.profile_image_url_https").first
        banner = selector.parse(user).jmespath("banner.image_url || legacy.profile_banner_url").first
        # The avatar comes from either place the API has put it; the banner the same way.
        if avatar:
            # "_normal" is the 48px version; without it the URL gives the picture as uploaded.
            original = avatar.replace("_normal.", ".")
            picture_id = PurePosixPath(urlsplit(original).path).parent.name
            name = f"avatar_{picture_id}{PurePosixPath(original).suffix or '.jpg'}"
            yield Post(user["rest_id"], [File(original, name=name)])
        if banner:
            yield Post(user["rest_id"], [File(f"{banner}/{BANNER_SIZE}",
                                              name=f"banner_{banner.rstrip('/').rsplit('/', 1)[-1]}.jpg")])

    def timeline(self, site: Site, account: Account, timeline: Timeline) -> Iterator[Post]:
        """One timeline's posts: from the API, and from the browser when the API can't give it.

        Args:
            site: the target's site.
            account: the user; its `browser` flag is set when this timeline is refused.
            timeline: which timeline to read.
        """
        if account.id is not None and not account.browser:
            refused = yield from self.api(site, account.id, timeline)
            if not refused:
                return
            account.browser = True
        yield from self.browser(site, account, timeline)

    def api(self, site: Site, user_id: str, timeline: Timeline) -> Generator[Post, None, bool]:
        """Read one timeline from the API to its end; the generator's value is True when the API refused it.

        Args:
            site: the target's site.
            user_id: the user's number.
            timeline: which timeline to read.

        Returns:
            True when the API refused the timeline (an old query id, say) and the browser should
            take over. A request that got no answer, or no JSON, is not a refusal: the browser
            would see the same, so it only warns and gives False.
        """
        seen: set[str] = set()

        def fetch(cursor: str | None) -> tuple[list[dict], str | None]:
            variables = {"userId": user_id, "count": PAGE_SIZE, "includePromotedContent": False, "withVoice": True}
            if cursor:
                variables["cursor"] = cursor
            tweets, following = read_timeline_page(self.graph.get(site, timeline.query, variables))
            # Every page repeats the pinned tweet, and the last one still has a bottom cursor,
            # so the end is the first page with no tweet not seen before.
            new = [tweet for tweet in tweets if tweet_id_of(tweet) not in seen]
            seen.update(tweet_id_of(tweet) for tweet in new)
            return new, following if new else None

        count = 0
        try:
            for new, _ in pages(fetch, lambda page: page[1]):
                count += 1
                for tweet in new:
                    yield from tweet_posts(tweet, timeline.category, user_id)
        except FetchError as error:
            # Only the answer's status tells a refusal from a connection that failed.
            if error.status is None:
                site.warn("{category}: {error}", category=timeline.category, error=error)
                return False
            site.warn("{category}: {error}; reading it in the browser", category=timeline.category, error=error)
            return True
        # A page that isn't JSON: the browser would get the same answer, so this is a warning, not a refusal.
        except http.Error as error:
            site.warn("{category}: {error}", category=timeline.category, error=error)
            return False
        site.info("{category}: {pages} read", category=timeline.category, pages=fmt.count(count, "page"))
        return False

    def browser(self, site: Site, account: Account, timeline: Timeline) -> Iterator[Post]:
        """Scroll one timeline's tab to its end, reading the same GraphQL responses the API gives.

        Args:
            site: the target's site.
            account: the user; without an id (unknown to the API) the replies can't be told apart
                from other people's, so none are kept, as before.
            timeline: which timeline's tab to scroll.
        """
        # A number is opened by its id under `/i/user/`; a screen name is opened under its own path.
        profile = f"https://x.com/i/user/{account.handle}" if account.handle.isdigit() else f"https://x.com/{account.handle}"
        found = 0
        # Watching starts before `goto`, so nothing of the first load is missed.
        with watch_responses(site, f"/{timeline.query}", scroll=True) as responses:
            if site.goto(profile + timeline.tab):
                for data in responses:
                    tweets, _ = read_timeline_page(data)
                    found += len(tweets)
                    for tweet in tweets:
                        yield from tweet_posts(tweet, timeline.category, account.id)
        site.info("{category}: {tweets} read in the browser", category=timeline.category,
                  tweets=fmt.count(found, "tweet"))


class Graph:
    """The GraphQL queries one run sends: their ids and feature flags, refreshed from x.com's script when stale.

        data = Graph().get(site, "UserMedia", {"userId": "42", "count": 50})

    It starts from the table the source's `state` kept from an earlier refresh, else from the
    built-in `QUERIES` and `FEATURES`. A refresh is tried once a run, on the first stale answer.
    """

    def __init__(self) -> None:
        # Each operation's id and flags, by its name; None until the first query reads `state`.
        self.queries: dict[str, dict[str, Any]] | None = None
        self.refreshed = False

    def get(self, site: Site, query: str, variables: dict) -> Any:
        """One GraphQL query's JSON; a stale query id refreshed once and the query sent again.

        Args:
            site: the target's site.
            query: a key of `QUERIES`.
            variables: the query's `variables`.

        Raises:
            FetchError: no answer, or a refusal (with its `status`) that a refresh didn't cure.
            http.Error: the answer isn't JSON.
        """
        try:
            return self._get(site, query, variables)
        # Only a stale id (400 or 404) is worth a refresh; any other refusal is raised as it is.
        except FetchError as error:
            if error.status not in STALE or self.refreshed or not self.refresh(site):
                raise
        return self._get(site, query, variables)

    def refresh(self, site: Site) -> bool:
        """Read the current query ids and flags from x.com's page and main script, once a run; False when it couldn't.

        Each operation takes exactly the flags its `featureSwitches` list, with the value the page
        gives each one; a switch the page doesn't name is sent as false, which X accepts (it refuses
        only a missing one). The table is kept in `state` for the next run.
        """
        # Once a run: the page and script cost two requests, so a second stale answer doesn't repeat them.
        self.refreshed = True
        # Not fatal: without a refresh the stale query fails with its own error, and the browser takes over.
        try:
            home = site.fetch(HOME).text
            found = MAIN_SCRIPT.search(home)
            if found is None:
                raise FetchError("x.com's page names no main script")
            script = site.fetch(found[0]).text
        except (FetchError, http.Error) as error:
            site.warn("the API's query ids look stale, and x.com's script couldn't be read: {error}", error=error)
            return False
        switches = {match["name"]: match["value"] == "true" for match in SWITCH.finditer(home)}
        queries = {}
        for match in OPERATION.finditer(script):
            if match["name"] in QUERIES:
                names = re.findall(r'"([\w]+)"', match["features"])
                queries[match["name"]] = {"id": match["id"], "features": {name: switches.get(name, False)
                                                                           for name in names}}
        if not queries:
            site.warn("the API's query ids look stale, and x.com's script lists none of them")
            return False
        self._table(site).update(queries)
        site.state[STATE_KEY] = dict(self._table(site))
        site.info("refreshed {n} from x.com's script", n=fmt.count(len(queries), "query id"))
        return True

    def _get(self, site: Site, query: str, variables: dict) -> Any:
        """One GraphQL query's JSON with the table's id and flags for it."""
        entry = self._table(site).get(query) or {}
        # The refreshed flags when the table has them, else the built-in ones.
        features = entry.get("features") or FEATURES
        return site.fetch(f"{API}{entry.get('id') or QUERIES[query]}/{query}", follow_redirects=False,
                          params={"variables": json.dumps(variables), "features": json.dumps(features)}).json

    def _table(self, site: Site) -> dict[str, dict[str, Any]]:
        """The query table: what `state` kept from an earlier refresh, read on first use."""
        if self.queries is None:
            kept = site.state.get(STATE_KEY)
            self.queries = {name: dict(entry) for name, entry in kept.items()
                            if isinstance(entry, dict) and name in QUERIES} if isinstance(kept, dict) else {}
        return self.queries


def read_timeline_page(data: Any) -> tuple[list[dict], str | None]:
    """One timeline page's tweets, and the cursor of the next page (None at the end).

    Args:
        data: one timeline query's JSON response.

    Returns:
        The page's tweets, wrapped or not, and the `Bottom` cursor that asks for the next page.
    """
    # The timeline sits under `user` in one response shape and under `timeline_v2` in the other.
    instructions = selector.parse(data).jmespath(
        "data.user.result.timeline.timeline.instructions || data.user.result.timeline_v2.timeline.instructions").all
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
    """Every entry of a page: added ones, the pinned one, and items added to a grid (the Media tab's later pages).

    Args:
        instructions: the page's `instructions` list.
    """
    for instruction in instructions:
        if isinstance(instruction, dict):
            yield from instruction.get("entries", [])
            # The Media tab's later pages come as `moduleItems`, not `entries`.
            yield from instruction.get("moduleItems", [])
            if "entry" in instruction:
                yield instruction["entry"]


def unwrap(tweet: dict) -> dict:
    """The tweet itself, out of the wrapper a tweet with limited visibility comes in.

    Args:
        tweet: a tweet object, wrapped or not.

    Returns:
        The inner `tweet` object when there is one, else `tweet` itself.
    """
    inner = tweet.get("tweet")
    return inner if isinstance(inner, dict) else tweet


def tweet_id_of(tweet: dict) -> str:
    """A tweet's id, inside its visibility wrapper or not.

    Args:
        tweet: a tweet object, wrapped or not.

    Returns:
        The id as text, from `rest_id` or else `legacy.id_str`; "" when neither is there.
    """
    tweet = unwrap(tweet)
    return str(tweet.get("rest_id") or selector.parse(tweet).jmespath("legacy.id_str").first or "")


def tweet_posts(tweet: dict, category: str, user_id: str | None, *, part: str | None = None) -> Iterator[Post]:
    """A tweet's photos and videos as posts, one each, when the category takes the tweet.

    Reposts take the retweeted tweet's files; replies take only the user's own replies; the other
    categories leave retweets out.

    Args:
        tweet: the tweet as the API gives it, wrapped or not.
        category: the category it is read for.
        user_id: whose timeline it is, to tell their replies from the tweets they answered; None
            when unknown, and then no reply is kept.
        part: the folder; None for the category's own, `images` or `videos` for the Media tab.
            "" files the media in the target's folder (one tweet).

    Each post has the tweet's id, its date, and `n`, the media's place in the tweet: it counts a
    media with no file (a GIF without MP4 variants) too, so a name never moves when one appears.
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
    if category == "replies" and (legacy.get("user_id_str") != user_id or not legacy.get("in_reply_to_status_id_str")):
        return
    tweet_id, created_at = tweet_id_of(tweet), legacy.get("created_at")
    posted = datetime.strptime(created_at, CREATED_AT) if created_at else None
    for index, media in enumerate(selector.parse(legacy).jmespath("extended_entities.media").all):
        found = best_file(media)
        if found is None:
            continue
        url, extension = found
        if part is None:
            folder = ("videos" if extension == "mp4" else "images") if category == "media" else category
        else:
            folder = part
        yield Post(tweet_id, [media_file(url, extension, tweet_id, index, posted)], date=posted, part=folder, n=index)


def media_file(url: str, extension: str, tweet_id: str, index: int, posted: datetime | None) -> File:
    """A media file, named from its URL's extension; pinned when the URL gives none the name could use.

    A URL path with no extension, or one in capitals, would be named from the content (or in
    lowercase) by the engine; the file was always named with `extension`, so then it keeps that.

    Args:
        url: the file's URL, which may carry a `?name=` query.
        extension: the extension to save it with when the URL gives none: "mp4", or "jpg" for a photo.
        tweet_id: the tweet's id, which the file name starts with.
        index: the media's place in the tweet, counted from 0.
        posted: the tweet's time, or None; the date is left out of the name when it is None.
    """
    suffix = PurePosixPath(urlsplit(url).path).suffix
    if suffix and suffix == suffix.lower():
        return File(url)
    stem = "_".join(str(part) for part in (day(posted), tweet_id, index) if part is not None)
    return File(url, name=f"{stem}.{extension}")


def best_file(media: dict) -> tuple[str, str] | None:
    """A photo at full size, or a video's highest-bitrate MP4, as `(url, extension)`.

    Args:
        media: one entry of a tweet's `extended_entities.media`.

    Returns:
        `(url, extension)`, or None for a video with no MP4 variant (only streaming playlists).
    """
    if media.get("type") == "photo" and media.get("media_url_https"):
        url = media["media_url_https"]
        # `name=orig` asks the media server for the full-size picture, not its thumbnail size.
        return f"{url}?name=orig", PurePosixPath(urlsplit(url).path).suffix.lstrip(".") or "jpg"
    variants = selector.parse(media).jmespath("video_info.variants").all
    # Only MP4 variants are kept: the streaming playlists aren't files the engine can save as an MP4.
    mp4s = [variant for variant in variants if variant.get("content_type") == "video/mp4"]
    if not mp4s:
        return None
    return max(mp4s, key=lambda variant: variant.get("bitrate", 0))["url"], "mp4"
