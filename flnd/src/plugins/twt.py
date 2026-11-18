"""Twitter / X on plugin API 2: a user's timelines and a search read in the browser, the profile
and one tweet from X's GraphQL API, each operation in its own bucket. See src/docs/plugins/twt.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, NamedTuple
from urllib.parse import urlencode, urlsplit

from flnd.plugin import (
    Browser,
    Changed,
    Ctx,
    File,
    NetworkError,
    NotFound,
    Plugin,
    Post,
    Range,
    Request,
    SiteError,
    Target,
    cookies,
    dig,
    route,
)

# The user agent Twitter's API has always been sent: a desktop Chrome.
CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/128.0.0.0 Safari/537.36")
# The web client's public bearer token: the same for everyone, sent by x.com's own script.
TWITTER_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
API = "https://x.com/i/api/graphql/"
# The web client's query ids, from its main.js (`queryId:"…",operationName:"…"`). They change now
# and then; a stale one is refreshed from x.com's script (`Graph.refresh`). The timelines'
# operations are named too, so a refresh keeps their ids, though only the browser sends them.
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
# The operation whose responses carry a search's tweets; only the browser sends it.
SEARCH = "SearchTimeline"
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
# queryId:"…",operationName:"UserMedia",operationType:"query",metadata:{featureSwitches:[...]}.
OPERATION = re.compile(
    r'queryId:"(?P<id>[\w-]+)",operationName:"(?P<name>\w+)",operationType:"\w+",'
    r"metadata:\{featureSwitches:\[(?P<features>[^\]]*)\]"
)
# A feature switch's value in the page's `__INITIAL_STATE__` (`featureSwitch.defaultConfig`, then
# the user's own config, which wins): `"rweb_video_screen_enabled":{"value":false}`.
SWITCH = re.compile(r'"(?P<name>[a-z0-9_]+)":\{"value":(?P<value>true|false)\b')
# 2026-11-12: a stale query id is answered 404 (an id the API no longer knows) or 400 (variables
# or features it no longer takes).
STALE = frozenset({400, 404})
# Where the refreshed table is kept in the plugin's `state`.
STATE_KEY = "graphql.queries"
# A tweet's `created_at`, as Twitter writes it: `Mon Nov 09 10:00:00 +0000 2026`, always UTC.
CREATED_AT = "%a %b %d %H:%M:%S %z %Y"
# The largest banner size the CDN serves, as the `/<size>` suffix of a banner's URL.
BANNER_SIZE = "1500x500"
# A card's image fields, best first: the full-size picture, then the smaller previews.
CARD_IMAGES = (
    "photo_image_full_size_original", "summary_photo_image_original", "player_image_original",
    "thumbnail_image_original", "photo_image_full_size_large", "summary_photo_image_large",
)
# The sizes X's media server gives a photo in, best first: `orig` is the upload itself.
PHOTO_SIZES = ("orig", "4096x4096", "large", "medium")
# Where a tweet's author's screen name is: under `core` since 2025, under `legacy` before.
AUTHOR = ("core.user_results.result.core.screen_name"
          " || core.user_results.result.legacy.screen_name")
# A tweet's links, read by the `post` route; `post` comes before `user`, so x.com/status/<id>
# is never the user "status".
TWEET_LINKS = tuple(
    f"{host}/{path}/{{tweet_id:\\d+}}"
    for host in ("x.com", "twitter.com") for path in ("*/status", "status", "i/web/status")
)


class Timeline(NamedTuple):
    """One timeline: the GraphQL operation its responses carry, and its tab under the profile."""

    query: str
    # The tab's path under the profile's URL; "" is the profile itself.
    tab: str


TIMELINES = {
    "posts": Timeline("UserOriginalsTimeline", ""),
    "media": Timeline("UserMedia", "/media"),
    "highlights": Timeline("UserHighlightsTweets", "/highlights"),
    "reposts": Timeline("UserRepostsTimeline", "/reposts"),
    "replies": Timeline("UserRepliesTimeline", "/with_replies"),
}


def _tab(category: str) -> str:
    """A timeline category's `url=`: its tab under the `page` the `user` route returns."""
    return "{page}" + TIMELINES[category].tab


class Twitter(Plugin):
    """Twitter / X users, single tweets and searches."""

    code = "twt"
    name = "Twitter/X"
    domains = ("x.com", "twitter.com")
    api = 2
    # Paths on the site that are pages of it, not profiles. x.com/i covers x.com/i/* too,
    # except the tweet and user-id links the routes read (more literal segments win).
    unsupported = {
        f"{host}/{path}": "that's not a profile"
        for host in ("x.com", "twitter.com")
        for path in (
            "home", "explore", "search", "settings", "notifications", "messages",
            "hashtag", "i", "intent", "compose", "login",
        )
    }
    # Not required by the code, but X usually sends a logged-out browser to its login page.
    login = cookies("auth_token", url="https://x.com/i/flow/login")
    # X's `x-rate-limit-*` headers hold each operation's bucket exactly at its limit, so the
    # floor only spaces a burst.
    pace = 0.2
    browser = Browser.ANY
    user_agent = CHROME
    # This browser never hid navigator.webdriver, so stealth stays off.
    stealth = False
    # Where X sends a tab it won't show without a login, or for a locked account.
    walls = ("/i/flow/login", "/login", "/account/access")
    # A post is one photo or video of a tweet, so a file is <date>_<tweet id>_<n> whatever the
    # tweet's media count; part is the category folder, images/videos for Media, "" for a tweet.
    folder = "{part}"
    fields = ("part", "n")
    filename = "{date}_{post}_{n}"
    key = "{post}_{n}"

    def __init__(self) -> None:
        # One instance serves every target of its account in a run: the query table, and the
        # tweets a search listed, by id, which `post` reads instead of asking the API.
        self.graph = Graph()
        self.found: dict[str, dict] = {}

    def sign(self, request: Request, cookies: Mapping[str, str]) -> Request:
        """Twitter's API headers on a request to `/i/api/`, with the current CSRF token."""
        # 2026-11-12: x.com's page and script (`Graph.refresh` reads them) answer 401 to these.
        if not urlsplit(request.url).path.startswith("/i/api/"):
            return request
        headers = {
            **(request.headers or {}),
            "authorization": TWITTER_BEARER,
            # The API checks that this equals the `ct0` cookie it also gets (double-submit CSRF).
            "x-csrf-token": cookies.get("ct0", ""),
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
        }
        if "auth_token" in cookies:
            headers["x-twitter-auth-type"] = "OAuth2Session"
        return request.replace(headers=headers)

    # -- Routes ------------------------------------------------------------------------------

    @route(*TWEET_LINKS, single=True)
    async def post(self, ctx: Ctx, tweet_id: str) -> AsyncIterator[Post]:
        """One tweet by its link, such as `https://x.com/example_handle/status/1790`: its media.

        A tweet a search of this run listed is read from the search, with no request.

        Raises:
            NotFound: the tweet is gone (404), or the API gives this login no tweet.
        """
        tweet = self.found.pop(tweet_id, None)
        if tweet is None:
            data = await self.graph.get(ctx, "TweetResultByRestId", {
                "tweetId": tweet_id, "withCommunity": False,
                "includePromotedContent": False, "withVoice": False,
            })
            tweet = dig(data, "data.tweetResult.result")
        # A protected tweet's own reason waits for a live check; until then it is NotFound.
        if not isinstance(tweet, dict) or not unwrap(tweet).get("legacy"):
            raise NotFound(f"twt: tweet {tweet_id} isn't there, or isn't visible to this login")
        for post in tweet_posts(tweet, "posts", None, part=""):
            yield post

    @route("twt?{query}")
    async def search(self, ctx: Ctx, query: str) -> AsyncIterator[Target]:
        """A search's tweets with media, such as `twt?example`, each as a tweet link.

        The Latest tab is scrolled in the browser. `when` is asked of X too, as `since:` and
        `until:`, so a search for a few days doesn't scroll through years.
        """
        words = " ".join([query, *range_operators(ctx.when)])
        url = "https://x.com/search?" + urlencode({"q": words, "src": "typed_query", "f": "live"})
        async for tweet, _ in self._scroll(ctx, url, SEARCH):
            if any(True for _ in tweet_posts(tweet, "posts", None, part="")):
                tweet_id = tweet_id_of(tweet)
                self.found[tweet_id] = tweet
                yield Target(f"https://x.com/i/web/status/{tweet_id}")

    @route("twt:{user}", "x.com/{user}", "twitter.com/{user}",
           r"x.com/i/user/{user:\d+}", r"twitter.com/i/user/{user:\d+}")
    async def user(self, ctx: Ctx, user: str) -> dict[str, Any]:
        """A profile by screen name or user id, such as `twt:example_handle`, read once.

        Returns:
            The profile every category gets: `handle`, `page` (the profile's URL), `user` (the
            API's user object, or None) and `refused` (the lookup's error, or None). A refused
            lookup leaves the timelines to the browser; `profile` raises it.

        Raises:
            LoginError: x.com refused the login (401 or 403).
        """
        profile: dict[str, Any] = {
            "handle": user, "user": None, "refused": None,
            "page": f"https://x.com/i/user/{user}" if user.isdigit() else f"https://x.com/{user}",
        }
        if user.isdigit():
            query, variables = "UserByRestId", {"userId": user}
        else:
            query, variables = "UserByScreenName", {"screen_name": user}
        try:
            found = dig(await self.graph.get(ctx, query, variables), "data.user.result")
        except (SiteError, NetworkError) as error:
            ctx.log("warn", "{query}: {error}", query=query, error=error)
            profile["refused"] = error
            return profile
        if isinstance(found, dict) and found.get("rest_id"):
            profile["user"] = found
        else:
            ctx.log("warn", "the API can't find @{name}; reading the profile in the browser",
                    name=user)
        return profile

    # -- The user's categories ---------------------------------------------------------------

    # A profile's tweets, its Media tab and its replies come by date, the pinned tweet apart (the
    # engine's 5-in-a-row rules cover it). Highlights and reposts come in the order they were
    # added, which is not the tweets' dates.
    @user.category(url=_tab("posts"))
    async def posts(self, ctx: Ctx, profile: dict[str, Any]) -> AsyncIterator[Post]:
        """The user's own tweets (retweets left out)."""
        async for post in self._timeline(ctx, profile, "posts"):
            yield post

    @user.category(url=_tab("media"))
    async def media(self, ctx: Ctx, profile: dict[str, Any]) -> AsyncIterator[Post]:
        """The Media tab: every photo and video, filed in images/ or videos/."""
        async for post in self._timeline(ctx, profile, "media"):
            yield post

    @user.category(url=_tab("highlights"), newest_first=False)
    async def highlights(self, ctx: Ctx, profile: dict[str, Any]) -> AsyncIterator[Post]:
        """The tweets the user highlighted."""
        async for post in self._timeline(ctx, profile, "highlights"):
            yield post

    @user.category(own=False, url=_tab("reposts"), newest_first=False)
    async def reposts(self, ctx: Ctx, profile: dict[str, Any]) -> AsyncIterator[Post]:
        """The media of the tweets the user retweeted, dated by the original tweet."""
        async for post in self._timeline(ctx, profile, "reposts"):
            yield post

    @user.category(url=_tab("replies"))
    async def replies(self, ctx: Ctx, profile: dict[str, Any]) -> AsyncIterator[Post]:
        """The user's own replies; the tweets they answered are left out (`wrote`)."""
        async for post in self._timeline(ctx, profile, "replies"):
            yield post

    # The pictures are named after their own id and keyed by their path, as always.
    @user.category(folder="profile", key="{path}", newest_first=False)
    async def profile(self, ctx: Ctx, profile: dict[str, Any]) -> AsyncIterator[Post]:
        """The avatar at its original size, and the banner at its largest.

        Raises:
            SiteError: the user lookup was refused (its own error), or NotFound when the API
                answered without the user: the pictures come from its user object.
        """
        user = profile["user"]
        if user is None:
            if profile["refused"] is not None:
                raise profile["refused"]
            raise NotFound(f"twt: the API has no user @{profile['handle']}, so no avatar or "
                           "banner")
        avatar = dig(user, "avatar.image_url || legacy.profile_image_url_https")
        banner = dig(user, "banner.image_url || legacy.profile_banner_url")
        if avatar:
            # "_normal" is the 48px version; without it the URL gives the picture as uploaded.
            original = avatar.replace("_normal.", ".")
            path = PurePosixPath(urlsplit(original).path)
            name = pin(original, f"avatar_{path.parent.name}", path.suffix.lstrip(".") or "jpg")
            yield Post(user["rest_id"], [File(original, name=name)], n=None)
        if banner:
            link = f"{banner}/{BANNER_SIZE}"
            name = pin(link, f"banner_{banner.rstrip('/').rsplit('/', 1)[-1]}", "jpg")
            yield Post(user["rest_id"], [File(link, name=name)], n=None)

    # -- Scrolling a tab ---------------------------------------------------------------------

    async def _timeline(self, ctx: Ctx, profile: dict[str, Any],
                        category: str) -> AsyncIterator[Post]:
        """Scroll one timeline's tab to its end, yielding the posts its category keeps."""
        timeline = TIMELINES[category]
        tab = profile["page"] + timeline.tab
        async for tweet, pinned in self._scroll(ctx, tab, timeline.query):
            for post in tweet_posts(tweet, category, profile):
                yield post.replace(pinned=True) if pinned else post

    async def _scroll(self, ctx: Ctx, url: str,
                      query: str) -> AsyncIterator[tuple[dict, bool]]:
        """Open a tab and scroll it to its end, yielding each new tweet of `query`'s responses,
        and whether it is the pinned one.

        The end is the site's: a page that brings no new tweet, or `timeline_state` reading a
        last page. The engine closes the generator sooner for its early stops.

        Raises:
            SiteError: the tab didn't load (NotFound for a 404), so nothing is recorded complete.
            LoginError: the tab landed on one of `walls`.
        """
        seen: set[str] = set()
        async with ctx.feed(f"/{query}", scroll=True, cursor=timeline_state) as responses:
            landed = await ctx.open(url)
            if landed.status >= 400:
                # A server error (a 503) may pass, so the engine's back-off tries it again.
                raise SiteError(f"twt: {url} answered HTTP {landed.status}",
                                status=landed.status, retryable=landed.status >= 500)
            async for data in responses:
                tweets, cursor, pinned = read_timeline_page(data)
                # 2026-11-16: a tombstone has no id; it is never new, nor stands for a seen one.
                new = [tweet for tweet in tweets
                       if (tid := tweet_id_of(tweet)) and tid not in seen]
                # Only a timeline page carries a cursor: an error or throttle body is skipped,
                # and a page of seen tweets is the end.
                if not new and cursor is None:
                    continue
                if not new:
                    break
                seen.update(tweet_id_of(tweet) for tweet in new)
                for tweet in new:
                    yield tweet, tweet_id_of(tweet) in pinned


class Graph:
    """The GraphQL queries one run sends: their ids and feature flags, refreshed when stale."""

    def __init__(self) -> None:
        # Each operation's id and flags, by its name; None until the first query reads `state`.
        self.queries: dict[str, dict[str, Any]] | None = None
        self.refreshed = False

    async def get(self, ctx: Ctx, query: str, variables: dict) -> Any:
        """One query's JSON, in the operation's own bucket; a stale id is refreshed once.

        Raises:
            SiteError: the API refused it, after a refresh when the refusal looked stale
                (NotFound for a 404, Blocked when its holds ran out).
            LoginError: x.com refused the login (401 or 403); never refreshed.
            NetworkError: no answer.
        """
        try:
            return await self._get(ctx, query, variables)
        # Only a stale id (400 or 404) is worth a refresh; any other refusal is raised as it is.
        except SiteError as error:
            if error.status not in STALE or self.refreshed or not await self.refresh(ctx):
                raise
        return await self._get(ctx, query, variables)

    async def refresh(self, ctx: Ctx) -> bool:
        """Read the current query ids and flags from x.com's page and main script, once a run.

        Returns:
            Whether the table was refreshed; not refreshing is a warning, and the query then
            fails with its own error.
        """
        # Once a run: two requests, so a second stale answer does not repeat them.
        self.refreshed = True
        try:
            home = (await ctx.fetch(HOME)).text
            found = MAIN_SCRIPT.search(home)
            if found is None:
                raise Changed("x.com's page names no main script")
            script = (await ctx.fetch(found[0])).text
        except (SiteError, NetworkError) as error:
            ctx.log("warn", "the API's query ids look stale, and x.com's script couldn't be "
                    "read: {error}", error=error)
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
            ctx.log("warn", "the API's query ids look stale, and x.com's script lists none of "
                    "them")
            return False
        self._table(ctx).update(queries)
        ctx.state[STATE_KEY] = dict(self._table(ctx))
        n = len(queries)
        ctx.log("info", "refreshed {n} from x.com's script",
                n=f"{n} query id" if n == 1 else f"{n} query ids")
        return True

    async def _get(self, ctx: Ctx, query: str, variables: dict) -> Any:
        """One query's JSON, with the table's id and flags, else `QUERIES`' and `FEATURES`."""
        entry = self._table(ctx).get(query) or {}
        features = entry.get("features") or FEATURES
        endpoint = f"{API}{entry.get('id') or QUERIES[query]}/{query}"
        # Compact JSON, as the web client sends it.
        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(features, separators=(",", ":")),
        }
        # X limits each operation on its own (its `x-rate-limit-*` headers), so each is a bucket.
        return (await ctx.fetch(Request(endpoint, params=params), bucket=query)).json

    def _table(self, ctx: Ctx) -> dict[str, dict[str, Any]]:
        """The query table: what `state` kept from an earlier refresh, read on first use.

        An entry for an operation flnd no longer sends, or one that isn't an object, is left
        out, so a damaged state can't break a query.
        """
        if self.queries is None:
            kept = ctx.state.get(STATE_KEY)
            self.queries = (
                {name: dict(entry) for name, entry in kept.items()
                 if isinstance(entry, dict) and name in QUERIES}
                if isinstance(kept, dict) else {}
            )
        return self.queries


# ---------------------------------------------------------------------------------------------
# Reading X's JSON
# ---------------------------------------------------------------------------------------------

def range_operators(when: Range) -> list[str]:
    """`when` as X search's operators: `since:` its first day, `until:` the day after its last,
    since X's `until:` is exclusive."""
    found = []
    if when.since:
        found.append(f"since:{when.since.isoformat()}")
    if when.until:
        found.append(f"until:{(when.until + timedelta(days=1)).isoformat()}")
    return found


def read_timeline_page(data: Any) -> tuple[list[dict], str | None, set[str]]:
    """One timeline page's tweets, wrapped as X sent them, its `Bottom` cursor (or None), and
    the id of its pinned tweet, if it has one.

    It reads a profile timeline in either response shape, and a search's. An entry is one
    tweet, or a module (a conversation, a grid row) whose items each hold one.
    """
    # 2026-11-11: a timeline sits under `timeline` in one response shape, `timeline_v2` in the
    # other; a search's under `search_by_raw_query`.
    instructions = dig(
        data,
        "data.user.result.timeline.timeline.instructions"
        " || data.user.result.timeline_v2.timeline.instructions"
        " || data.search_by_raw_query.search_timeline.timeline.instructions",
    )
    tweets: list[dict] = []
    cursor, pinned = None, set()
    for entry, pin in timeline_entries(instructions if isinstance(instructions, list) else []):
        content = entry.get("content") or entry.get("item") or {}
        # The `Bottom` cursor is the request for the next page; the `Top` one is not used.
        if content.get("cursorType") == "Bottom":
            cursor = content.get("value")
        items = [item.get("item", item) for item in content.get("items", [])] or [content]
        for item in items:
            tweet = dig(item, "itemContent.tweet_results.result")
            if isinstance(tweet, dict):
                tweets.append(tweet)
                if pin:
                    pinned.add(tweet_id_of(tweet))
    return tweets, cursor, pinned


def timeline_state(data: Any) -> tuple[bool, str | None] | None:
    """`(more, cursor)` of one timeline page, for `ctx.feed(cursor=…)`; None for another body.

    A body without a `Bottom` cursor (an error or throttle body) says nothing. A page with a
    cursor but no tweets is the end: X answers the last page with its cursors alone.
    """
    tweets, cursor, _ = read_timeline_page(data)
    if cursor is None:
        return None
    return bool(tweets), cursor


def timeline_entries(instructions: list[Any]) -> Iterator[tuple[dict, bool]]:
    """Every entry of a page (added ones, the pinned one, items added to a grid), and whether
    it is the pinned one."""
    for instruction in instructions:
        if isinstance(instruction, dict):
            for entry in instruction.get("entries", []):
                yield entry, False
            # 2026-11-11: the Media tab's later pages come as `moduleItems`, not `entries`.
            for entry in instruction.get("moduleItems", []):
                yield entry, False
            # A pinned tweet (`TimelinePinEntry`), and a search's later cursors
            # (`TimelineReplaceEntry`), come as one `entry`.
            if "entry" in instruction:
                yield instruction["entry"], instruction.get("type") == "TimelinePinEntry"


def unwrap(tweet: dict) -> dict:
    """The tweet itself, out of the wrapper a tweet with limited visibility comes in."""
    inner = tweet.get("tweet")
    return inner if isinstance(inner, dict) else tweet


def tweet_id_of(tweet: dict) -> str:
    """A tweet's id, inside its visibility wrapper or not; "" for a tombstone."""
    tweet = unwrap(tweet)
    return str(tweet.get("rest_id") or dig(tweet, "legacy.id_str") or "")


def wrote(profile: dict[str, Any] | None, tweet: dict) -> bool:
    """Whether `tweet` is the profile's user's own: by the user's id when it is known, else by
    the author's screen name a timeline's tweets carry (2026-11-16)."""
    if profile is None:
        return False
    tweet = unwrap(tweet)
    user, handle = profile["user"], profile["handle"]
    user_id = user["rest_id"] if user else (handle if handle.isdigit() else None)
    if user_id is not None:
        return (tweet.get("legacy") or {}).get("user_id_str") == user_id
    author = dig(tweet, AUTHOR)
    return isinstance(author, str) and author.lower() == handle.lower()


def parse_date(created_at: Any) -> datetime | None:
    """A `created_at` as an aware UTC datetime; None when there is none or it has another shape
    (2026-11-16), which leaves the tweet undated rather than failing its timeline."""
    if not created_at:
        return None
    try:
        return datetime.strptime(created_at, CREATED_AT)
    except (TypeError, ValueError):
        return None


def tweet_posts(tweet: dict, category: str, profile: dict[str, Any] | None, *,
                part: str | None = None) -> Iterator[Post]:
    """A tweet's photos and videos as posts, one each, when the category takes the tweet.

    Reposts take the retweeted tweet's files; replies take only the user's own replies; the
    other categories leave retweets out. A quoted tweet's media (`n` = `q0`, `q1`, …) and a
    card's image (`n` = `card`) come with the tweet's own.

    Args:
        tweet: the tweet, wrapped or not.
        category: the category it is listed for, which decides what it keeps and its folder.
        profile: the `user` route's profile, for `replies`; None elsewhere.
        part: the folder for every file (`""` for a tweet link); None files them by category.
    """
    tweet = unwrap(tweet)
    legacy = tweet.get("legacy") or {}
    retweeted = dig(legacy, "retweeted_status_result.result")
    if category == "reposts":
        if not isinstance(retweeted, dict):
            return
        tweet = unwrap(retweeted)
        legacy = tweet.get("legacy") or {}
    # Retweets are left out of every other category: their media is filed under reposts.
    elif retweeted is not None:
        return
    if category == "replies" and (
            not wrote(profile, tweet) or not legacy.get("in_reply_to_status_id_str")):
        return
    tweet_id = tweet_id_of(tweet)
    posted = parse_date(legacy.get("created_at"))
    quoted = dig(tweet, "quoted_status_result.result")
    quoted_media = (dig(unwrap(quoted).get("legacy") or {}, "extended_entities.media")
                    if isinstance(quoted, dict) else None)
    found: list[tuple[int | str, dict]] = list(enumerate(listed(
        dig(legacy, "extended_entities.media"))))
    found += [(f"q{index}", media) for index, media in enumerate(listed(quoted_media))]
    # `created_at` is UTC, so its day is the one the engine gives `{date}`.
    day = posted.astimezone(UTC).date().isoformat() if posted else None
    for index, media in found:
        best = best_file(media)
        if best is None:
            continue
        urls, extension = best
        if part is not None:
            folder = part
        elif category == "media":
            folder = "videos" if extension == "mp4" else "images"
        else:
            folder = category
        name = pin(urls[0], stem(day, tweet_id, index), extension)
        file = File(mirrors=urls, name=name) if len(urls) > 1 else File(urls[0], name=name)
        yield Post(tweet_id, [file], date=posted, part=folder, n=index)
    card = card_image(tweet)
    if card is not None:
        folder = part if part is not None else ("images" if category == "media" else category)
        file = File(card, name=pin(card, stem(day, tweet_id, "card"), "jpg"))
        yield Post(tweet_id, [file], date=posted, part=folder, n="card")


def stem(day: str | None, tweet_id: str, index: int | str) -> str:
    """What `filename` makes of a tweet's file: `<day>_<tweet id>_<n>`, without an absent day."""
    return "_".join(str(part) for part in (day, tweet_id, index) if part is not None)


def pin(url: str, stem: str, extension: str) -> str:
    """Today's full name, which the content never changes: `<stem>` with the URL's lowercase
    extension, else with `extension` as written (`.JPG` stays `.JPG`)."""
    suffix = PurePosixPath(urlsplit(url).path).suffix
    return stem + (suffix if suffix and suffix == suffix.lower() else f".{extension}")


def listed(value: Any) -> list[dict]:
    """The dicts of a JSON list; [] for anything else."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def card_image(tweet: dict) -> str | None:
    """The image of a tweet's card (the preview X draws for a link), at its largest; or None."""
    images = {
        entry.get("key"): dig(entry, "value.image_value.url")
        for entry in listed(dig(tweet, "card.legacy.binding_values"))
    }
    return next((images[key] for key in CARD_IMAGES if images.get(key)), None)


def best_file(media: dict) -> tuple[Sequence[str], str] | None:
    """A photo's URLs at its sizes, best first, or a video's highest-bitrate MP4, with the
    extension it is saved with; None for a media with no file.

    A GIF is a video to X: it comes as an MP4 variant, so it is saved as one.
    """
    if media.get("type") == "photo" and media.get("media_url_https"):
        # `name=orig` asks for the full-size picture; the smaller sizes are mirrors for when it
        # refuses one, tried in this order.
        url = media["media_url_https"]
        extension = PurePosixPath(urlsplit(url).path).suffix.lstrip(".") or "jpg"
        return [f"{url}?name={size}" for size in PHOTO_SIZES], extension
    # 2026-11-11: only MP4 variants are kept; streaming playlists aren't files saved as MP4.
    mp4s = [variant for variant in listed(dig(media, "video_info.variants"))
            if variant.get("content_type") == "video/mp4"]
    if not mp4s:
        return None
    return [max(mp4s, key=lambda variant: variant.get("bitrate", 0))["url"]], "mp4"
