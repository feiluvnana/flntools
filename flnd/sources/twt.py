"""Twitter / X: a user's posts, media, highlights, reposts, replies and profile pictures, one
tweet by its link, and a search's tweets.

    flnd download twt:example_handle --only posts,profile
    flnd download https://x.com/example_handle/status/1790000000000000000
    flnd download "twt?example words"

Who calls it: the engine (`flnd.engine.job`), through the routes and categories below, on the
target's
listing thread. What it calls: only its `Site` (`site.fetch` for X's GraphQL API, the browser
verbs for the timelines) and the stdlib.

How a target is read:
1. A `user` target (`twt:<handle>`, `twt:<user id>`, a profile link) asks the API for the
   profile once (`UserByScreenName` or `UserByRestId`), and hands the `Account` to each wanted
   category. When the API can't find the user, the account has no API object, and the
   timelines are still read in the browser.
2. Each timeline category (`posts`, `media`, `highlights`, `reposts`, `replies`) opens its tab
   of the profile and scrolls it, reading the JSON of the tab's GraphQL responses as they come
   (`site.feed`). The tabs read after it start loading in background tabs meanwhile.
3. Each tweet becomes one `Post` per photo or video (and per quoted tweet's media, and per card
   image), filed by `tweet_posts`.
4. The `profile` category yields the avatar and the banner from the API's user object.
5. A `post` target (a tweet link) asks the API for the one tweet (`TweetResultByRestId`).
6. A `search` target (`twt?<words>`) scrolls the search's Latest tab in the browser and yields
   a link to each tweet with media, which the engine downloads as a `post` target.

Every GraphQL call goes through `Graph`, which keeps the web client's query ids. X changes them
now and then; a refused id (400 or 404) is refreshed once a run from x.com's main script.

How a timeline ends: X answers its last page with its cursors alone, which `timeline_state`
reads as "no next page", and the feed stops at once. A page that brings no tweet not seen
before ends the scroll too. A body without a `Bottom` cursor (an error or throttle answer) is
neither: it is skipped and the scroll goes on.

How login works: `login = cookies("auth_token")`, not required, though X usually sends a
logged-out browser to its login page. `flnd login twt` reads X's cookies from a browser or a
cookie file; the engine loads them into the HTTP client and the browser. API calls also need
`ct0`, which X sets itself and checks as a CSRF token (`sign`). A tab that lands on one of
`WALLS` (the login flow, or the locked-account page) stops the target with a `LoginError`; it
isn't waited out. A 401 or 403 from the API is a `LoginError` too.

Site quirks, each with the day it was first seen (flnd/docs/sources/twt has them for people):
- 2026-11-11: a timeline sits under `timeline` in one response shape and `timeline_v2` in the
  other; the Media tab's later pages come as `moduleItems`, not `entries`.
- 2026-11-11: a tweet with limited visibility comes inside a wrapper (`unwrap`).
- 2026-11-11: an avatar URL ending in `_normal` is the 48 px copy; the banner's largest size is
  `BANNER_SIZE`. A video's streaming playlists are left out: only its MP4 variants are files.
- 2026-11-12: query ids go stale, answered 404 (an id the API no longer knows) or 400 (variables
  or features it no longer takes); `Graph.refresh` reads the current ones from x.com's script.
- 2026-11-12: x.com's page and script answer 401 to the API's bearer token, so it goes only to
  `/i/api/`.
- 2026-11-16: a removed tweet (a tombstone) has no id; an error or throttle body carries no
  cursor. Neither ends a scroll.
- 2026-11-16: a `created_at` in another shape than `CREATED_AT` leaves its tweet undated.
- Since 2025 (day not recorded): a tweet's author's screen name moved from `legacy` to `core`;
  both are read (`AUTHOR`).

Rules a change must keep:
- The engine owns the early stops (a sync caught up, `--when`): a timeline yields every tweet
  in listing order, and ends only when the site's feed does (a page with no new tweet, or the
  feed's own cursor saying there is no more).
- The names and history keys of saved files never change: `filename` and `key` below, and the
  `n` field (`0`, `1`, `q0`, `card`), make `<date>_<tweet id>_<n>` and `<tweet id>_<n>`. A
  changed key makes every saved file download again.
- The API's bearer token and CSRF header go only to `/i/api/` (`sign`): x.com's page and
  script answer 401 to them.

Under .flnd/media/twt/<target>/:
  posts/, highlights/, reposts/, replies/   <date>_<tweet_id>_<n>.<ext>
  images/, videos/                          the Media tab's photos and videos
  profile/                                  avatar_<id>.jpg and banner_<id>.jpg
A tweet link's files sit in its own target folder, `twt/<tweet id>/`. See flnd/docs/sources/twt.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, NamedTuple
from urllib.parse import urlencode, urlsplit

from flnd.source import (
    Changed,
    File,
    LoginError,
    NetworkError,
    Post,
    Request,
    Site,
    SiteError,
    Source,
    Target,
    cookies,
    dig,
    target,
)

# The user agent Twitter's API has always been sent: a desktop Chrome.
CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/128.0.0.0 Safari/537.36")
# Where X sends a tab it won't show without a login, or for a locked account.
WALLS = ("/i/flow/login", "/login", "/account/access")
# The web client's public bearer token: the same for everyone, sent by x.com's own script.
TWITTER_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
API = "https://x.com/i/api/graphql/"
# The web client's query ids, from its main.js (`queryId:"…",operationName:"…"`). They change now
# and then; a stale one is refreshed from x.com's script (`Graph.refresh`). The timelines'
# operations are named here too, so a refresh keeps their ids, though only the browser sends them.
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
# The operation whose responses carry a search's tweets; only the browser sends it, so its id
# never matters to flnd.
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
# A feature switch's value in the page's `__INITIAL_STATE__` (`featureSwitch.defaultConfig` and the
# user's own config after it, which wins): `"rweb_video_screen_enabled":{"value":false}`.
SWITCH = re.compile(r'"(?P<name>[a-z0-9_]+)":\{"value":(?P<value>true|false)\b')
# What a stale query id is answered with: a 404 for an id the API no longer knows, a 400 for a
# query whose variables or features it no longer takes.
STALE = frozenset({400, 404})
# Where the refreshed table is kept in the source's `state`.
STATE_KEY = "graphql.queries"
# A tweet's `created_at`, as Twitter writes it: `Mon Nov 09 10:00:00 +0000 2026`, always UTC.
# The engine reads ISO dates and timestamps only, so the source reads this one itself.
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


class Timeline(NamedTuple):
    """One timeline: its category, its GraphQL operation, and its tab under the profile."""

    # Category posts are filed under: "posts", "media", "highlights", "reposts" or "replies".
    category: str
    # The GraphQL operation whose responses carry the timeline; the feed watches for it.
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
    """What the `user` route gives every category: who is read, and the API's user object."""

    # The screen name or the number the target was given.
    handle: str
    # The API's user object; None when the API can't find them, and the browser reads everything.
    user: dict | None

    @property
    def id(self) -> str | None:
        """The user's id: the API's, or the target itself when it is a number; else None."""
        if self.user:
            return self.user["rest_id"]
        return self.handle if self.handle.isdigit() else None

    def wrote(self, tweet: dict) -> bool:
        """Whether `tweet` is the user's own.

        By the user's id when it is known; else by the tweet's author's screen name, which a
        timeline's tweets carry, so the `replies` tab can still tell the user's replies from
        the tweets they answered when the API couldn't find the user.

        Args:
            tweet: a tweet from a timeline, wrapped or not.

        Returns:
            True for the user's own tweet; False for another's, or one whose author can't be
            read.
        """
        tweet = unwrap(tweet)
        if self.id is not None:
            return (tweet.get("legacy") or {}).get("user_id_str") == self.id
        author = dig(tweet, AUTHOR)
        return isinstance(author, str) and author.lower() == self.handle.lower()


class Twitter(Source):
    """Twitter / X users, single tweets and searches: the API for the profile and one tweet,
    the browser for the timelines and the search."""

    code = "twt"
    name = "Twitter/X"
    domains = ("x.com", "twitter.com")
    api = 1
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
    login = cookies("auth_token")
    # X's `x-rate-limit-*` headers stop flnd exactly at the limit, so the floor only spaces a burst.
    pace = 0.2
    user_agent = CHROME
    # This browser never hid navigator.webdriver, so stealth stays off.
    stealth = False
    # A post is one photo or video of a tweet, so a file is <date>_<tweet id>_<n> whatever the
    # tweet's media count; part is the category folder, images/videos for Media, "" for a tweet.
    folder = "{part}"
    fields = ("part", "n")
    filename = "{date}_{post}_{n}"
    key = "{post}_{n}"

    def __init__(self) -> None:
        # The GraphQL query table for this run; one source instance serves the whole run.
        self.graph = Graph()

    def sign(self, request: Request, /) -> None:
        """Twitter's API headers on every request to x.com's API, with the current CSRF token.

        Args:
            request: a request to x.com or twitter.com; only one to `/i/api/` is changed.
        """
        # The page and the script (`Graph.refresh` reads them) answer 401 to the bearer token.
        if not urlsplit(request.url).path.startswith("/i/api/"):
            return
        request.headers["authorization"] = TWITTER_BEARER
        # The API checks that this equals the `ct0` cookie it also receives (double-submit CSRF).
        request.headers["x-csrf-token"] = request.cookies.get("ct0", "")
        request.headers["x-twitter-active-user"] = "yes"
        request.headers["x-twitter-client-language"] = "en"
        if "auth_token" in request.cookies:
            request.headers["x-twitter-auth-type"] = "OAuth2Session"

    # -----------------------------------------------------------------------------------------
    # Routes
    # -----------------------------------------------------------------------------------------

    # `post` comes before `user`, so x.com/status/<id> is never the user "status".
    @target(r"x.com/*/status/{tweet_id:\d+}", r"twitter.com/*/status/{tweet_id:\d+}",
            r"x.com/status/{tweet_id:\d+}", r"twitter.com/status/{tweet_id:\d+}",
            r"x.com/i/web/status/{tweet_id:\d+}", r"twitter.com/i/web/status/{tweet_id:\d+}")
    def post(self, site: Site, tweet_id: str) -> Iterator[Post]:
        """One tweet, by its id: its media, in the target's own folder.

        Args:
            site: the target's site.
            tweet_id: the tweet's id, from its link.

        Yields:
            One post per photo, video and card image; nothing, with a warning, when the API
            refuses the tweet or doesn't show it to this login.

        Raises:
            LoginError: x.com refused the login (401 or 403).
        """
        variables = {
            "tweetId": tweet_id, "withCommunity": False,
            "includePromotedContent": False, "withVoice": False,
        }
        try:
            data = self.graph.get(site, "TweetResultByRestId", variables)
        except (SiteError, NetworkError) as error:
            site.warn("tweet {id}: {error}", id=tweet_id, error=error)
            return
        tweet = dig(data, "data.tweetResult.result")
        if not isinstance(tweet, dict) or not unwrap(tweet).get("legacy"):
            site.warn("tweet {id} isn't there, or isn't visible to this login", id=tweet_id)
            return
        yield from tweet_posts(tweet, "posts", None, part="")

    @target("twt?{query}")
    def search(self, site: Site, query: str) -> Iterator[Target]:
        """A search's tweets with media, newest first, each as a tweet link to download.

        The search's Latest tab is scrolled in the browser, as the timelines are. `--when`
        is asked of X too, as the search's `since:` and `until:` operators, so a search for a
        few days doesn't scroll through years.

        A query route always yields targets: each tweet is then downloaded as a `post`
        target, into its own folder `twt/<tweet id>/`, named and keyed as a tweet link is, so a
        tweet saved before is skipped by its history keys.

        Args:
            site: the target's site.
            query: the words, as typed after `twt?`; X's own operators (`from:`, `filter:`)
                work too.

        Yields:
            `Target("https://x.com/i/web/status/<id>")` for each tweet with a file.

        Raises:
            LoginError: X sent the tab to its login page (search needs a login).
        """
        words = [query, *range_operators(site)]
        url = "https://x.com/search?" + urlencode(
            {"q": " ".join(words), "src": "typed_query", "f": "live"}
        )
        for tweet in self._scroll(site, url, SEARCH):
            if any(True for _ in tweet_posts(tweet, "posts", None, part="")):
                yield Target(f"https://x.com/i/web/status/{tweet_id_of(tweet)}")

    @target("twt:{user}", "x.com/{user}", "twitter.com/{user}",
            r"x.com/i/user/{user:\d+}", r"twitter.com/i/user/{user:\d+}")
    def user(self, site: Site, user: str) -> Account:
        """A profile by screen name or numeric user id, read once for every category.

        Args:
            site: the target's site.
            user: the screen name, or the user's id.

        Returns:
            The `Account`; its `user` is None (with a warning) when the API can't find them,
            and the timelines are read in the browser anyway.

        Raises:
            LoginError: x.com refused the login (401 or 403).
        """
        # A number is a user id, so it is asked by `UserByRestId`; anything else is a screen name.
        if user.isdigit():
            query, variables = "UserByRestId", {"userId": user}
        else:
            query, variables = "UserByScreenName", {"screen_name": user}
        found = None
        try:
            found = dig(self.graph.get(site, query, variables), "data.user.result")
        except (SiteError, NetworkError) as error:
            site.warn("{query}: {error}", query=query, error=error)
        if isinstance(found, dict) and found.get("rest_id"):
            return Account(user, found)
        site.warn("the API can't find @{name}; reading the profile in the browser", name=user)
        return Account(user, None)

    # -----------------------------------------------------------------------------------------
    # The user's categories
    # -----------------------------------------------------------------------------------------

    # Newest first: a profile's tweets, its Media tab and its replies come by date, the pinned
    # tweet apart (the engine's 5-in-a-row rules cover it). Highlights and reposts come in the
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
        """The user's own replies; the tweets they answered are left out.

        Without the API's user object, the user's replies are told apart by the author's
        screen name (`Account.wrote`), so the category still lists them.

        Args:
            site: the target's site.
            account: whose replies they are, as the `user` route found them.

        Yields:
            A post per photo and video of each of the user's own replies.
        """
        yield from self.read_browser(site, account, TIMELINES["replies"])

    # The pictures are named after the picture's own id and keyed by their path, as always.
    @user.category(folder="profile", key="")
    def profile(self, site: Site, account: Account) -> Iterator[Post]:
        """The avatar at its original size, and the banner at its largest.

        Nothing when the API couldn't find the user: the pictures come from its user object.

        Args:
            site: the target's site; nothing is fetched here, the engine downloads the files.
            account: the account the `user` route found.

        Yields:
            A post for the avatar, then one for the banner, each when the user has it; both
            have the user's id as their post id.
        """
        if account.user is None:
            return
        user = account.user
        # The avatar comes from either place the API has put it; the banner the same way.
        avatar = dig(user, "avatar.image_url || legacy.profile_image_url_https")
        banner = dig(user, "banner.image_url || legacy.profile_banner_url")
        if avatar:
            # "_normal" is the 48px version; without it the URL gives the picture as uploaded.
            original = avatar.replace("_normal.", ".")
            picture_id = PurePosixPath(urlsplit(original).path).parent.name
            name = f"avatar_{picture_id}{PurePosixPath(original).suffix or '.jpg'}"
            yield Post(user["rest_id"], [File(original, name=name)])
        if banner:
            banner_name = f"banner_{banner.rstrip('/').rsplit('/', 1)[-1]}.jpg"
            yield Post(user["rest_id"], [File(f"{banner}/{BANNER_SIZE}", name=banner_name)])

    # -----------------------------------------------------------------------------------------
    # Scrolling a tab
    # -----------------------------------------------------------------------------------------

    def read_browser(self, site: Site, account: Account, timeline: Timeline) -> Iterator[Post]:
        """Scroll one timeline's tab to its end, yielding its posts as they come.

        The tabs listed after this one in `TIMELINES` start loading in background tabs once
        this one is open, so the next category finds its page ready.

        Args:
            site: the target's site.
            account: whose timeline it is; a numeric handle opens `/i/user/<id>`.
            timeline: the tab to read, and the operation its responses carry.

        Yields:
            A post per photo and video of each tweet the category keeps (`tweet_posts`).

        Raises:
            LoginError: the tab landed on X's login page or its locked-account page.
        """
        # A number is opened by its id under `/i/user/`; a screen name under its own path.
        profile = (
            f"https://x.com/i/user/{account.handle}"
            if account.handle.isdigit()
            else f"https://x.com/{account.handle}"
        )
        # The tabs after this one start loading in the background once this one is open.
        names = list(TIMELINES)
        later = [profile + TIMELINES[name].tab
                 for name in names[names.index(timeline.category) + 1:]]
        for tweet in self._scroll(site, profile + timeline.tab, timeline.query, warm=later):
            yield from tweet_posts(tweet, timeline.category, account)

    def _scroll(
        self, site: Site, url: str, query: str, *, warm: list[str] | None = None
    ) -> Iterator[dict]:
        """Open a tab and scroll it to its end, yielding each new tweet of `query`'s responses.

        The end is the site's own: a page that brings no new tweet, or the feed's cursor
        reader (`timeline_state`) seeing a last page. The engine closes the generator sooner
        for its early stops.

        Args:
            site: the target's site.
            url: the tab to open.
            query: the GraphQL operation whose responses carry the tab's tweets.
            warm: tabs to start loading in the background once this one is open.

        Yields:
            Each tweet, the first time it is seen.

        Raises:
            LoginError: the tab landed on X's login page or its locked-account page.
        """
        seen: set[str] = set()
        with site.feed(f"/{query}", scroll=True, cursor=timeline_state) as responses:
            if not site.open(url):
                return
            path = urlsplit(site.url).path
            # A whole segment: a handle like `login_example` only starts with `/login`.
            if any(path == wall or path.startswith(f"{wall}/") for wall in WALLS):
                raise LoginError(
                    "twt: X asked for a login or locked this account; "
                    "check it in a browser or run flnd login twt",
                    hint="run flnd login twt",
                )
            if warm:
                site.warm(warm)
            for data in responses:
                tweets, cursor = read_timeline_page(data)
                # A tombstone has no id; it is never new, and never stands for a seen tweet.
                new = [
                    tweet for tweet in tweets
                    if (tid := tweet_id_of(tweet)) and tid not in seen
                ]
                # Only a timeline page carries a cursor: an error or throttle body is skipped,
                # and a page of seen tweets is the end.
                if not new and cursor is None:
                    continue
                if not new:
                    break
                seen.update(tweet_id_of(tweet) for tweet in new)
                yield from new


class Graph:
    """The GraphQL queries one run sends: their ids and feature flags, refreshed when stale."""

    def __init__(self) -> None:
        # Each operation's id and flags, by its name; None until the first query reads `state`.
        self.queries: dict[str, dict[str, Any]] | None = None
        self.refreshed = False

    def get(self, site: Site, query: str, variables: dict) -> Any:
        """One GraphQL query's JSON; a stale query id is refreshed once and the query sent again.

        Args:
            site: the target's site.
            query: the operation's name, a key of `QUERIES`.
            variables: the operation's variables.

        Returns:
            The response's JSON.

        Raises:
            SiteError: the API refused it, after a refresh when the refusal looked stale.
            LoginError: x.com refused the login (401 or 403); never refreshed.
            NetworkError: no answer.
        """
        try:
            return self._get(site, query, variables)
        # Only a stale id (400 or 404) is worth a refresh; any other refusal is raised as it is.
        except SiteError as error:
            if error.status not in STALE or self.refreshed or not self.refresh(site):
                raise
        return self._get(site, query, variables)

    def refresh(self, site: Site) -> bool:
        """Read the current query ids and flags from x.com's page and main script, once a run.

        Args:
            site: the target's site.

        Returns:
            Whether the table was refreshed. Not refreshing is a warning, not an error: the
            query then fails with its own error.
        """
        # Once a run: two requests, so a second stale answer does not repeat them.
        self.refreshed = True
        try:
            home = site.fetch(HOME).text
            found = MAIN_SCRIPT.search(home)
            if found is None:
                raise Changed("x.com's page names no main script")
            script = site.fetch(found[0]).text
        except (SiteError, NetworkError) as error:
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
        n = len(queries)
        site.info("refreshed {n} from x.com's script",
                  n=f"{n} query id" if n == 1 else f"{n} query ids")
        return True

    def _get(self, site: Site, query: str, variables: dict) -> Any:
        """One GraphQL query's JSON, with the table's id and flags for it.

        The id and flags come from the refreshed table when it has the operation, else from
        `QUERIES` and `FEATURES`. Both JSON values are sent compact, as the web client does.

        Args:
            site: the target's site; `sign` adds the API's headers.
            query: the operation's name, a key of `QUERIES`.
            variables: the operation's variables.

        Returns:
            The response's JSON.
        """
        entry = self._table(site).get(query) or {}
        # The refreshed flags when the table has them, else the built-in ones.
        features = entry.get("features") or FEATURES
        endpoint = f"{API}{entry.get('id') or QUERIES[query]}/{query}"
        # Compact JSON, as the web client sends it.
        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(features, separators=(",", ":")),
        }
        return site.fetch(endpoint, params=params).json

    def _table(self, site: Site) -> dict[str, dict[str, Any]]:
        """The query table: what `state` kept from an earlier refresh, read on first use.

        An entry for an operation flnd no longer sends, or one that isn't an object, is left
        out, so a damaged state can't break a query.

        Args:
            site: the target's site, whose `state` holds the table under `STATE_KEY`.

        Returns:
            `{operation: {"id": ..., "features": {...}}}`, the same object on every call of
            the run; empty before any refresh.
        """
        if self.queries is None:
            kept = site.state.get(STATE_KEY)
            self.queries = (
                {name: dict(entry) for name, entry in kept.items()
                 if isinstance(entry, dict) and name in QUERIES}
                if isinstance(kept, dict) else {}
            )
        return self.queries


# ---------------------------------------------------------------------------------------------
# Reading X's JSON
# ---------------------------------------------------------------------------------------------

def range_operators(site: Site) -> list[str]:
    """`--when` as X search's operators: `since:` from its first day, `until:` after its last.

    X's `until:` is exclusive, so it is the day after `--when`'s last.

    Args:
        site: the target's site, whose `time` is `--when`.

    Returns:
        The operators to add to the search's words; empty without `--when`.
    """
    found = []
    if site.time.since:
        found.append(f"since:{site.time.since.isoformat()}")
    if site.time.until:
        found.append(f"until:{(site.time.until + timedelta(days=1)).isoformat()}")
    return found


def read_timeline_page(data: Any) -> tuple[list[dict], str | None]:
    """One timeline page's tweets, and the cursor of the next page (None for none).

    It reads a profile timeline in either response shape, and a search's. An entry is one
    tweet, or a module (a conversation, a grid row) whose items each hold one.

    Args:
        data: a response's JSON; anything that isn't a timeline page gives no tweets.

    Returns:
        `(tweets, cursor)`: the tweets in page order, wrapped as X sent them, and the
        `Bottom` cursor's value, or None when the page has none.
    """
    # The timeline sits under `timeline` in one response shape and `timeline_v2` in the other;
    # a search's under `search_by_raw_query`.
    instructions = dig(
        data,
        "data.user.result.timeline.timeline.instructions"
        " || data.user.result.timeline_v2.timeline.instructions"
        " || data.search_by_raw_query.search_timeline.timeline.instructions",
    )
    tweets, cursor = [], None
    for entry in timeline_entries(instructions if isinstance(instructions, list) else []):
        content = entry.get("content") or entry.get("item") or {}
        # The page's `Bottom` cursor is the request for the next page; the `Top` one is not used.
        if content.get("cursorType") == "Bottom":
            cursor = content.get("value")
        items = [item.get("item", item) for item in content.get("items", [])] or [content]
        for item in items:
            tweet = dig(item, "itemContent.tweet_results.result")
            if isinstance(tweet, dict):
                tweets.append(tweet)
    return tweets, cursor


def timeline_state(data: Any) -> tuple[bool, str | None] | None:
    """Whether one timeline page has a next page, and its cursor, for `site.feed(cursor=…)`.

    A body without a `Bottom` cursor (an error or throttle body) says nothing. A page with a
    cursor but no tweets is the end: X answers the last page with its cursors alone.

    Args:
        data: a response's JSON.

    Returns:
        `(more, cursor)`, or None for a body that isn't a timeline page.
    """
    tweets, cursor = read_timeline_page(data)
    if cursor is None:
        return None
    return bool(tweets), cursor


def timeline_entries(instructions: list[Any]) -> Iterator[dict]:
    """Every entry of a page: added ones, the pinned one, and items added to a grid.

    Args:
        instructions: the page's `instructions` list; an item that isn't an object is skipped.

    Yields:
        Each entry, in the order the instructions give them.
    """
    for instruction in instructions:
        if isinstance(instruction, dict):
            yield from instruction.get("entries", [])
            # The Media tab's later pages come as `moduleItems`, not `entries`.
            yield from instruction.get("moduleItems", [])
            # A pinned tweet (`TimelinePinEntry`), and a search's later cursors
            # (`TimelineReplaceEntry`), come as one `entry`.
            if "entry" in instruction:
                yield instruction["entry"]


def unwrap(tweet: dict) -> dict:
    """The tweet itself, out of the wrapper a tweet with limited visibility comes in."""
    inner = tweet.get("tweet")
    return inner if isinstance(inner, dict) else tweet


def tweet_id_of(tweet: dict) -> str:
    """A tweet's id, inside its visibility wrapper or not; "" for a tombstone."""
    tweet = unwrap(tweet)
    return str(tweet.get("rest_id") or dig(tweet, "legacy.id_str") or "")


def parse_date(created_at: Any) -> datetime | None:
    """A `created_at` as a datetime, or None when there is none or it has another shape.

    Args:
        created_at: the tweet's `legacy.created_at`, such as
            `Mon Nov 09 10:00:00 +0000 2026`; None, "" or another type is no date.

    Returns:
        An aware datetime in UTC, or None.
    """
    if not created_at:
        return None
    try:
        return datetime.strptime(created_at, CREATED_AT)
    # Another shape leaves the tweet undated rather than failing its whole timeline.
    except (TypeError, ValueError):
        return None


def tweet_posts(
    tweet: dict, category: str, account: Account | None, *, part: str | None = None,
) -> Iterator[Post]:
    """A tweet's photos and videos as posts, one each, when the category takes the tweet.

    Reposts take the retweeted tweet's files; replies take only the user's own replies; the
    other categories leave retweets out. A quoted tweet's media (`n` = `q0`, `q1`, …) and a
    card's image (`n` = `card`) come with the tweet's own.

    Args:
        tweet: the tweet, wrapped or not.
        category: the category it is listed for, which decides what it keeps and its folder.
        account: whose timeline it is, for `replies`; None elsewhere.
        part: the folder for every file (`""` for a tweet link); None files them by category.

    Yields:
        One post per file, its `n` field the file's place in the tweet.
    """
    tweet = unwrap(tweet)
    legacy = tweet.get("legacy") or {}
    retweeted = dig(legacy, "retweeted_status_result.result")
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
        account is None
        or not account.wrote(tweet)
        or not legacy.get("in_reply_to_status_id_str")
    ):
        return
    tweet_id = tweet_id_of(tweet)
    posted = parse_date(legacy.get("created_at"))
    quoted = dig(tweet, "quoted_status_result.result")
    quoted_media = (
        dig(unwrap(quoted).get("legacy") or {}, "extended_entities.media")
        if isinstance(quoted, dict) else None
    )
    own_media = dig(legacy, "extended_entities.media")
    found_media: list[tuple[int | str, dict]] = list(enumerate(listed(own_media)))
    found_media += [(f"q{index}", media) for index, media in enumerate(listed(quoted_media))]
    for index, media in found_media:
        found = best_file(media)
        if found is None:
            continue
        url, extension = found
        if part is not None:
            folder = part
        elif category == "media":
            folder = "videos" if extension == "mp4" else "images"
        else:
            folder = category
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


def listed(value: Any) -> list[dict]:
    """The dicts of a JSON list; [] for anything else."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def card_image(tweet: dict) -> str | None:
    """A tweet's card image, at its largest; None when it has no card or no image.

    A card is the preview X draws for a link in a tweet. Its images sit in
    `card.legacy.binding_values`, by size; the first of `CARD_IMAGES` it has wins.

    Args:
        tweet: the tweet, out of its wrapper.

    Returns:
        The image's URL, or None.
    """
    images = {
        entry.get("key"): dig(entry, "value.image_value.url")
        for entry in listed(dig(tweet, "card.legacy.binding_values"))
    }
    return next((images[key] for key in CARD_IMAGES if images.get(key)), None)


def media_file(
    url: str | tuple[str, ...], extension: str, tweet_id: str, index: int | str,
    posted: datetime | None,
) -> File:
    """A media file, named from its URL's extension; its name pinned when the URL has none.

    A URL with no extension, or one in capitals, would leave the engine to sniff the content;
    the name `<date>_<tweet id>_<n>.<extension>` is what the templates would have given.

    Args:
        url: the file's URL, or its mirrors, best first; the first names the extension.
        extension: the extension to give a URL without one, without a dot.
        tweet_id: the tweet's id, part of the pinned name.
        index: the file's place in the tweet (`0`, `q0`, `card`), part of the pinned name.
        posted: when the tweet was posted; None leaves the date out of the pinned name.

    Returns:
        The file, named by the engine's templates when its URL has a lowercase extension, else
        with its name pinned.
    """
    suffix = PurePosixPath(urlsplit(url if isinstance(url, str) else url[0]).path).suffix
    if suffix and suffix == suffix.lower():
        return File(url)
    # `created_at` is UTC, so its day is the one the engine gives `{date}`.
    posted_day = posted.astimezone(UTC).date().isoformat() if posted else None
    stem = "_".join(str(part) for part in (posted_day, tweet_id, index) if part is not None)
    return File(url, name=f"{stem}.{extension}")


def best_file(media: dict) -> tuple[str | tuple[str, ...], str] | None:
    """A photo with mirror URLs, or a video's highest-bitrate MP4, as (url, extension).

    A GIF is a video to X: it comes as an MP4 variant, so it is saved as one.

    Args:
        media: one entry of a tweet's `extended_entities.media`.

    Returns:
        `(urls, extension)` for a photo, its sizes in `PHOTO_SIZES` order as mirrors;
        `(url, "mp4")` for a video; None for a media with no file flnd can save.
    """
    if media.get("type") == "photo" and media.get("media_url_https"):
        url = media["media_url_https"]
        # `name=orig` asks the media server for the full-size picture, not its thumbnail size; the
        # smaller sizes are mirrors for when it refuses one, tried in this order.
        sizes = tuple(f"{url}?name={size}" for size in PHOTO_SIZES)
        return sizes, PurePosixPath(urlsplit(url).path).suffix.lstrip(".") or "jpg"
    # Only MP4 variants are kept: streaming playlists aren't files the engine can save as MP4.
    mp4s = [variant for variant in listed(dig(media, "video_info.variants"))
            if variant.get("content_type") == "video/mp4"]
    if not mp4s:
        return None
    return max(mp4s, key=lambda variant: variant.get("bitrate", 0))["url"], "mp4"
