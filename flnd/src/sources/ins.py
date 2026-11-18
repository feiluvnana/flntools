"""Instagram: a profile's posts, reels, tagged posts, reposts, stories, highlights and picture,
one post or reel by its link, and every account the login follows.

    flnd download ins:example_user --only posts,stories
    flnd download https://www.instagram.com/p/C1abcDEFghi/
    flnd download ins:me

Who calls it: the engine (`flnd.engine.crawl`), through the routes and categories below, on a thread
of the run's (`SourceRun.call`). What it calls: only its `Site` (`site.fetch` for Instagram's REST
API, the browser verbs for the profile's tabs and the post and story pages) and the stdlib.

How a profile (`ins:example_user`) is read:
1. The `user` route returns an empty `Profile`; nothing is loaded until a category needs it.
2. Each tab category (`posts`, `reels`, `tagged`, `reposts`) opens its tab and scrolls it,
   reading the GraphQL results the page embeds (`embedded_results`) and the ones its scroll
   brings (`site.feed`). The scroll ends when the tab's own connection says there is no next
   page (`page_state`). The tabs read after it start loading in background tabs meanwhile.
3. A grid item that is the whole post (`is_complete`) is yielded with its files. Any other is
   yielded where it is listed as a lazy post, dated by its id (`pk_stamp`): the engine asks for
   its files only when the post isn't saved yet, and they come from the media API by its `pk`.
   A post the API can't give needs its own page, which can't be opened in the tab while that
   tab scrolls, so it is listed again once the scroll ends (`Profile.read_tab`).
4. `stories` and `highlights` load the profile page once for the highlights (`Profile.describe`),
   then read the story and highlight pages directly.
5. `profile` yields the largest picture the profile page's results name.

A post link (`post` route) asks the media API by the `pk` its code spells, then reads the
post's page. `ins:me` pages through the friendships API and yields each followed account as a
target, which the engine downloads.

How a call reaches the API (`Api`): flnd's own client first, with the web client's headers
(`sign`). Instagram may refuse that client (a 400, 401 or 403, or a login wall in its JSON):
the first refusal moves every later call of the run into the logged-in tab
(`site.fetch(..., in_page=True)`), with one warning. A "please wait" answer is waited out with
`site.back_off`; a login wall twice, then the call counts as refused; `/accounts/suspended/` is
a hard stop.

Routes and categories:
- `post` (`instagram.com/p/<code>`, `/reel/`, `/reels/`, `/tv/`, also under a profile path):
  one post, whole, with no category. Its patterns come before `user`'s, so `/p/` is no user.
- `me` (`ins:me`): discovers every followed account as a `user` target; needs a login.
- `user` (`ins:<name>`, `instagram.com/<name>`, a tab link too) with the categories `posts`,
  `reels`, `tagged`, `reposts` (the four tabs, in that order), `stories`, `highlights` and
  `profile`. `posts`, `reels` and `reposts` are newest first, so the engine may stop them
  early; `tagged` and the story categories are read whole.

How a listing ends: a tab ends when its connection's `page_info` says there is no next page,
read from the embedded first page or from a scrolled response (`page_state`); the feed's own
end (a quiet bottom) ends it too. `ins:me` ends on a page without `next_max_id`, or the same one
again. Stories and highlights are one answer (or one page) each.

Login: `cookies("sessionid")`, not required. `flnd login ins` reads a browser's cookies or a
cookie file; the run loads them into the HTTP client and the browser. Logged out, the same pages
are read and Instagram shows what it shows the public, which is little. `ins:me` needs the
account's id (`Api._self_id`: the `ds_user_id` cookie, the `sessionid` prefix, or the API).

Site quirks, each with the date it was seen (the ins page in src/docs/sources/ has them all):
- Throttling is a JSON answer, `"Please wait a few minutes..."`, not a status (2026-11).
- flnd's own client may be refused (by its TLS fingerprint, it seems) while the tab's fetch
  is answered, so calls move into the tab (2026-11-12).
- `x-ig-set-www-claim` rotates and must be echoed as `X-IG-WWW-Claim` (2026-11-12).
- The story and reels REST APIs fail with HTTP 500 on live Instagram, so stories and
  highlights are read directly from their pages (2026-11).
- The profile page stopped embedding the user and its highlights; its own GraphQL calls bring
  them (2026-11).
- A grid item may carry only a thumbnail; an image under 90% of `original_width` is read again
  (2026-11-12).
- Ids hold their creation time, so a lazy grid post is dated by its `pk` (2026-11-12). A
  private post's code is longer than 11 letters and spells no `pk` (2026-11-12).
- The reels and reposts tabs have used two connection names each; the reels tab wraps each post
  as `{"media": post}`, reposts may come as `repost_grid_items` (2026-11-11).
- Up to 3 pinned posts sit above newer ones in the grid (2026-11-14).
- A story's `id` carries its owner after an `_` (2026-11-11); its music comes in two shapes
  (2026-11-13).
- A throttled session is sent to `/accounts/login` or `/challenge`, a wall that lifts after a
  wait; `/accounts/suspended` is a suspended or locked account (2026-11).

Rules a change must keep:
- The engine owns the early stops (a sync caught up, `--when`): a tab yields its posts in
  listing order and ends only where the tab's own `page_info` says so.
- The tab must never be navigated while it scrolls: its feed watches that page. A lazy post's
  files open no page during a scroll (see the third step above).
- The names and history keys of saved files never change: `<date>_<code>[_<n>]` and
  `<code>_<n>` for posts, the file's path for stories, highlights and the picture. A changed
  key makes every saved file download again.

Folder layout under `.flnd/media/ins/<username>/`:
  posts/, reels/, tagged/, reposts/   <date>_<code>[_<n>].jpg (or .mp4)
  stories/, highlights/<title>/       <date>_<id>.jpg (or .mp4), <date>_<id>_music.m4a
  profile/                            the CDN's own file name
A post link's files sit in its own target folder, `ins/<code>/`. The guide for people is the
ins page in src/docs/sources/.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from flnd.source import (
    Blocked,
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

SITE = "https://www.instagram.com"
# The web client's REST API, which gallery-dl and Instaloader read too.
API = f"{SITE}/api/v1"
# The profile's tabs, in the order they are read, and their paths under the profile.
TABS = {"posts": "", "reels": "reels/", "tagged": "tagged/", "reposts": "reposts/"}
# The CDN refuses a file request that doesn't come from the site.
CDN_HEADERS = {"Referer": f"{SITE}/"}
# Where each profile tab's result lists its posts; the name Instagram uses now comes first.
TAB_CONNECTIONS = {
    "posts": ("xdt_api__v1__feed__user_timeline_graphql_connection",),
    "reels": ("fetch__XDTUserDict.clips_connection", "xdt_api__v1__clips__user__connection_v2"),
    "tagged": ("xdt_api__v1__usertags__user_id__feed_connection",),
    "reposts": (
        "fetch__XDTUserDict.user_reposts_timeline",
        "xdt_api__v1__feed__user_reposts_graphql_connection",
    ),
}
IMAGE_EXTENSIONS = {"jpg", "webp", "png", "heic"}
# One post's page: a reel's, or any other post's.
POST = "data.xig_fetch_media || data.xdt_api__v1__media__shortcode__web_info.items[0]"
# A story page lists one user's active stories; a highlight's page lists the highlight's.
STORIES = ("data.xdt_api__v1__feed__reels_media__connection.edges[].node.items[]"
           " || data.xdt_api__v1__feed__reels_media.reels_media[].items[]")
# What Instagram's JSON says when it is rate limiting, seen 2026-11:
# {"message": "Please wait a few minutes before you try again.", "status": "fail"}.
THROTTLED = "wait a few minutes"
# The web client's app id and ASBD id, which its API calls carry (seen 2026-11).
APP_ID = "936619743392459"
ASBD_ID = "129477"
# Where Instagram sends a session it stopped: a suspended or locked account (a hard stop), and the
# login wall or challenge it puts up while it throttles (waited out).
SUSPENDED = "/accounts/suspended"
WALLS = ("/accounts/login", "/challenge")
# Login walls waited out in a row before a page or a call counts as walled for good.
WALL_ATTEMPTS = 2
# What an API answer's `message` holds when it is a login wall rather than a result.
CHECKPOINTS = ("checkpoint_required", "challenge_required", "login_required")
# Accounts asked for in one page of the friendships API, as the web client asks.
FOLLOWING_COUNT = 100
# A grid image within this share of the post's `original_width` is its full size, not a thumbnail.
FULL_SIZE = 0.9
# Instagram ids carry creation time: milliseconds since this moment (2011-08-24 21:07:01.721
# UTC), above the lowest 23 bits (the shard and a sequence number).
EPOCH_MS = 1314220021721
ID_SHIFT = 23
# A post's code is its `pk` in base 64, in this alphabet; a longer code (a private post's) isn't.
CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
LONGEST_CODE = 11
# The statuses by which Instagram refuses flnd's own client but answers the tab's fetch.
REFUSED_DIRECT = frozenset({400, 401, 403})
# The statuses of a post or user that isn't there: an answer, not an error.
GONE = frozenset({404, 410})
# What a GraphQL call's URL holds: the responses that carry a profile tab's or a post's data.
GRAPHQL = "graphql"
# Where a story's music is, in the two shapes Instagram sends it (as gallery-dl reads it): a music
# sticker's asset, or the story's own music metadata.
STORY_MUSIC = ("story_music_stickers[0].music_asset_info.progressive_download_url || "
               "music_metadata.music_info.music_asset_info.progressive_download_url")
# A desktop Chrome's User-Agent for flnd's own client, which Instagram expects of its web API.
# The version only needs to be one Instagram still serves; it isn't matched to the browser's.
CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
LOGIN_HINT = "run flnd login ins"


class Instagram(Source):
    """Instagram profiles, single posts and the login's follows: the API first, then the pages."""

    code = "ins"
    name = "Instagram"
    domains = ("instagram.com",)
    api = 1
    # Logged in when `sessionid` is there. Not required: a public profile shows something.
    login = cookies("sessionid")
    user_agent = CHROME
    # At least 0.2 s between requests, page loads included: Instagram publishes no limit, and
    # its throttle answers are waited out (`Api.get`), so this only keeps bursts apart.
    pace = 0.2
    # It reads in the browser: one of its targets lists at a time (the engine's `HTTP_AT_ONCE`
    # is for a source that only sends requests).
    browser = True
    # A post's category, then a highlight's title: "posts", "highlights/<title>".
    folder = "{category}/{album}"
    fields = ("album",)
    # The day, the code, and the file's number when the post has more than one: the names saved
    # files have always had.
    filename = "{date}_{post}{index}"
    # Every story link, a user's or a highlight's: a story expires, but the profile lists them all.
    unsupported = {
        "instagram.com/stories": (
            "story links aren't supported; give the profile, and its stories come with it"
        ),
        "instagram.com/explore": "that's not a profile",
        "instagram.com/accounts": "that's not a profile",
        "instagram.com/direct": "that's not a profile",
    }

    def __init__(self) -> None:
        # How this run reaches the API; one source instance serves the whole run.
        self.api = Api()

    def sign(self, request: Request) -> None:
        """Add the web client's API headers to a request to instagram.com.

        Args:
            request: the request; its `cookies` give the CSRF token.
        """
        request.headers.update(self.api.headers(request.cookies))

    # `post` comes before `user`, so instagram.com/p/<code> is never the user "p". A post's code is
    # base64url, so it takes `-` as well as `\w`.
    @target(
        r"instagram.com/p/{code:[\w-]+}", r"instagram.com/reel/{code:[\w-]+}",
        r"instagram.com/reels/{code:[\w-]+}", r"instagram.com/tv/{code:[\w-]+}",
        r"instagram.com/*/p/{code:[\w-]+}", r"instagram.com/*/reel/{code:[\w-]+}",
        r"instagram.com/*/reels/{code:[\w-]+}", r"instagram.com/*/tv/{code:[\w-]+}",
    )
    def post(self, site: Site, code: str) -> Iterator[Post]:
        """One post or reel by its code: from the media API, else from its page.

        Args:
            site: the target's site.
            code: the post's code, as in its link.

        Yields:
            The post, with its files; nothing when neither the API nor the page has it.
        """
        media = self.api.post(site, code)
        if media is not None:
            yield post_of(media)

    @target("ins:{me:me}", shown="ins:me", discovers=True)
    def me(self, site: Site, me: str) -> Iterator[Target]:
        """Every account the login follows, each as a target the engine downloads.

        Args:
            site: the target's site.
            me: always "me".

        Yields:
            `Target("ins:<user name>")` for each account, in the API's order.

        Raises:
            LoginError: no login, so no account to read.
        """
        yield from self.api.following(site)

    @target("ins:{user}", "instagram.com/{user}")
    def user(self, site: Site, user: str) -> Profile:
        """A profile, by its user name; a tab's URL (/reels/, /tagged/) is its profile too.

        Nothing is loaded yet: a tab, or the first category that needs the profile's page, loads it.

        Args:
            site: the target's site.
            user: the user name, as the URL gives it.

        Returns:
            The empty `Profile` every category receives.
        """
        return Profile(user)

    @user.category(newest_first=True)
    def posts(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile's posts, newest first (up to 3 pinned ones above), to the end of the grid.

        Args:
            site: the target's site.
            profile: the user's `Profile`, which its tabs fill in as they are read.

        Yields:
            Each post, whole or lazy, in the grid's order.
        """
        yield from profile.read_tab(site, self.api, "posts")

    @user.category(newest_first=True)
    def reels(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile's reels, newest first."""
        yield from profile.read_tab(site, self.api, "reels")

    @user.category
    def tagged(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The posts the profile is tagged in, in the order the tab lists them."""
        yield from profile.read_tab(site, self.api, "tagged")

    @user.category(newest_first=True)
    def reposts(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The posts the profile reposted, newest first."""
        yield from profile.read_tab(site, self.api, "reposts")

    @user.category(key="")
    def stories(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile's active stories, from the story page; keyed by path.

        Args:
            site: the target's site.
            profile: the user's `Profile`.

        Yields:
            One post per story, its music beside it.
        """
        profile.describe(site)
        yield from story_posts(site, f"{SITE}/stories/{profile.user}/", "stories")

    @user.category(key="")
    def highlights(self, site: Site, profile: Profile) -> Iterator[Post]:
        """Each highlight's stories, in the highlight's own folder; keyed by path.

        Args:
            site: the target's site.
            profile: the user's `Profile`; its page is loaded for the highlights if no tab was.

        Yields:
            One post per story of each highlight, its title as the `album` field.
        """
        profile.describe(site)
        found = profile.highlights
        site.warm([f"{SITE}/stories/highlights/{highlight}/" for highlight, _ in found])
        for highlight, title in found:
            # The engine makes the title safe for a folder name (`album` in `folder`).
            where = f"highlights/{title}"
            for post in story_posts(site, f"{SITE}/stories/highlights/{highlight}/", where):
                yield post.replace(album=title)

    @user.category(folder="profile", key="")
    def profile(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile picture, at its largest, under the CDN's own name; keyed by path."""
        profile.describe(site)
        if profile.picture:
            picture = File(profile.picture, name=picture_name(profile.picture))
            yield Post(profile.user, [picture], headers=CDN_HEADERS)


# ---------------------------------------------------------------------------------------------
# The REST API
# ---------------------------------------------------------------------------------------------

class Api:
    """How one run reaches Instagram's REST API: its headers, its claim, and where calls go from.

        media = Api().media(site, "3141592653589793238")      # a post, or None when it isn't there

    Every call waits out a throttle (`site.back_off`) and calls again, and stops hard on
    `/accounts/suspended/`. The first refusal of flnd's own client (a 400, 401 or 403, or a
    login wall) moves every later call into the logged-in tab, with one warning.
    """

    def __init__(self) -> None:
        # The `X-IG-WWW-Claim` Instagram gave last, echoed on the next calls.
        self.claim = "0"
        # Whether calls go from the tab (`fetch(in_page=True)`), after flnd's client was refused.
        self.in_page = False
        # The APIs Instagram refused this run, so a refusal is said once and never asked again.
        self.refused: set[str] = set()

    def headers(self, cookies: Mapping[str, str]) -> dict[str, str]:
        """The headers Instagram's web client sends its API.

        Args:
            cookies: the login's cookies; `csrftoken` becomes `X-CSRFToken`.

        Returns:
            The headers, by name.
        """
        sent = {"X-IG-App-ID": APP_ID, "X-ASBD-ID": ASBD_ID, "X-IG-WWW-Claim": self.claim,
                "X-Requested-With": "XMLHttpRequest", "Referer": f"{SITE}/"}
        if cookies.get("csrftoken"):
            sent["X-CSRFToken"] = cookies["csrftoken"]
        return sent

    def get(self, site: Site, url: str, *, params: Any = None) -> Any | None:
        """One API call's JSON, every throttle waited out; None for a post or user not there.

            data = api.get(site, f"{API}/media/{pk}/info/")

        The call is made with `check=False`, so a refusal comes back as its response: its body
        says whether it is a throttle, a login wall, or flnd's client being refused, which moves
        the calls into the tab (`_waited`). So does an answer that is a page instead of JSON:
        Instagram didn't treat the call as an API call, and the tab's own fetch is. `site.fetch`
        would otherwise raise a 401 or 403 as a `LoginError`, and the tab's fetch would never
        be tried.

        Args:
            site: the target's site.
            url: the API URL.
            params: the query, as a mapping or as pairs (a repeated `reel_ids`).

        Returns:
            The parsed JSON; None for a 404 or 410.

        Raises:
            SiteError: a refusal, from the client or the tab (`Blocked` for a 429), or a login
                wall that stayed; `Changed` for an answer that isn't JSON. The callers fall back
                to the pages.
            NetworkError: no answer, after the client's tries.
            LoginError: Instagram sent the call to `/accounts/suspended/`.
        """
        walls = 0
        # Each pass makes one call; a throttle, a login wall or the move into the tab calls again.
        while True:
            headers = self.headers(site.cookies) if self.in_page else None
            response = site.fetch(url, params=params, in_page=self.in_page, headers=headers,
                                  check=False)
            if response.status in GONE:
                return None
            refused = response.status in REFUSED_DIRECT
            next_walls = self._waited(site, response, refused=refused, walls=walls)
            if next_walls is not None:
                walls = next_walls
                continue
            if not response.ok:
                raise refusal(url, response)
            # Instagram rotates this claim and expects it echoed back on the next calls.
            self.claim = response.headers.get("x-ig-set-www-claim") or self.claim
            try:
                return response.json
            except Changed:
                if self.in_page:
                    # An answer that isn't JSON from the tab is an HTML error or login page:
                    # the caller falls back to the post's page.
                    raise Changed(f"{url}: Instagram's answer isn't JSON") from None
                # A page instead of an answer is Instagram not treating the call as an API
                # call; the tab's own fetch is (seen 2026-11-17). Call again from the tab.
                self.in_page = True
                site.warn("Instagram answered the API with a page; sending its API calls "
                          "from the page")

    def media(self, site: Site, pk: str) -> dict | None:
        """One post by `pk`, from `/api/v1/media/<pk>/info/`.

            media = api.media(site, "3141592653589793238")

        Args:
            site: the target's site.
            pk: the post's id, as digits.

        Returns:
            The post's JSON; None when it isn't there, or the API was refused from the tab
            earlier this run.

        Raises:
            SiteError, NetworkError, LoginError: as `get`. A refusal, or a page answer, from
                the tab marks the API refused for the rest of the run.
        """
        if "media" in self.refused:
            return None
        try:
            data = self.get(site, f"{API}/media/{pk}/info/")
        except (SiteError, NetworkError):
            if self.in_page:
                self.refused.add("media")
            raise
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        return None

    def post(self, site: Site, code: str) -> dict | None:
        """One post by its code: from the media API by the `pk` it spells, else from its page.

            media = api.post(site, "C1abcDEFghi")

        Args:
            site: the target's site.
            code: the post's code, as in its URL.

        Returns:
            The post's JSON as a post page shows it; None when neither the API nor the page has it.
        """
        pk = pk_of_code(code)
        media = None
        if pk:
            # A refused media API isn't fatal: the post's page, read below, still holds the post.
            try:
                media = self.media(site, pk)
            except Changed:
                # Instagram answers the media endpoint with a page, as it answers the API
                # (seen 2026-11-17): `get` announced the move into the page once, and the
                # post's page is the source now. Saying it again per post is noise.
                pass
            except (SiteError, NetworkError) as error:
                site.warn("post {code}: media API refused ({error}); reading page",
                          code=code, error=error)
        # A code that decodes to another post (a private post's longer code) is never taken for it.
        if media is not None and media.get("code") == code:
            return media
        return post_page(site, code)

    def following(self, site: Site) -> Iterator[Target]:
        """Every account the login follows, page by page from the friendships API.

        The listing ends on the API's own word: a page without `next_max_id`, or the same one
        again.

        Args:
            site: the target's site.

        Yields:
            `Target("ins:<user name>")` for each account.

        Raises:
            LoginError: no login to read the account's id from.
            Changed: an answer without its `users`.
            SiteError, NetworkError: as `get`.
        """
        user_id = self._self_id(site)
        max_id: str | None = None
        while True:
            params: dict[str, Any] = {"count": FOLLOWING_COUNT}
            if max_id is not None:
                params["max_id"] = max_id
            data = self.get(site, f"{API}/friendships/{user_id}/following/", params=params)
            if not isinstance(data, dict) or not isinstance(data.get("users"), list):
                raise Changed("ins:me: Instagram's following list has no users")
            for user in data["users"]:
                username = user.get("username") if isinstance(user, dict) else None
                if isinstance(username, str) and username:
                    yield Target(f"ins:{username}")
            next_id = data.get("next_max_id")
            if not next_id or str(next_id) == max_id:
                return
            max_id = str(next_id)

    def _self_id(self, site: Site) -> str:
        """The login's numeric user id: its `ds_user_id` cookie, its `sessionid`, or the API.

        Args:
            site: the target's site.

        Returns:
            The id, as digits.

        Raises:
            LoginError: none of the three gives one: there is no login.
        """
        user_id = site.cookies.get("ds_user_id", "")
        if user_id.isdigit():
            return user_id
        # A `sessionid` starts with its account's id: `<id>%3A<rest>`.
        prefix = unquote(site.cookies.get("sessionid", "")).split(":", 1)[0]
        if prefix.isdigit():
            return prefix
        data = self.get(site, f"{API}/accounts/current_user/", params={"edit": "true"})
        user = data.get("user") if isinstance(data, dict) else None
        found = (user.get("pk") or user.get("id")) if isinstance(user, dict) else None
        if found:
            return str(found)
        raise LoginError("ins: not logged in, so there is no account to read", hint=LOGIN_HINT)

    def _waited(self, site: Site, response: Any, *, refused: bool, walls: int) -> int | None:
        """Wait out what `response` says, or move the calls into the tab; None to take it as is.

        In order: a suspended account is a hard stop; flnd's client refused (`refused`, or a
        login wall in the body) moves the calls into the tab, once; a throttle is waited out;
        a login wall from the tab is waited out `WALL_ATTEMPTS` times, then is a refusal.

        Args:
            site: the target's site.
            response: the call's answer, a refusal's included.
            refused: its status is one by which Instagram refuses flnd's own client.
            walls: the login walls waited out in a row so far for this call.

        Returns:
            The walls waited out so far when the call should be made again; None when the
            answer is the call's result (or its refusal).

        Raises:
            LoginError: the call was sent to `/accounts/suspended/`.
            SiteError: a login wall stayed after its waits.
        """
        # The suspended page is the hard stop: nothing is retried past it.
        where = urlsplit(response.url).path
        if where.startswith(SUSPENDED):
            raise suspended()
        data = _json(response)
        message = str(data.get("message", "")).lower() if isinstance(data, dict) else ""
        walled = (is_wall(where) or (isinstance(data, dict) and bool(data.get("require_login")))
                  or any(key in message for key in CHECKPOINTS))
        if (refused or walled) and not self.in_page and not throttled(data):
            # Instagram may refuse flnd's client (by its TLS fingerprint, it seems), while the
            # tab's fetch is the page's own (seen 2026-11-12).
            self.in_page = True
            site.warn("Instagram refused flnd's own client; sending its API calls from the page")
            return walls
        if throttled(data):
            site.back_off("Instagram asked to wait a few minutes")
            return walls
        if walled:
            if walls >= WALL_ATTEMPTS:
                # A SiteError, not a LoginError: the caller falls back to the post's page, which
                # a wall on the API alone doesn't stop. A listing it ends is a login problem.
                raise SiteError(f"{response.url}: Instagram showed a login wall",
                                hint=f"{LOGIN_HINT}, then try again")
            site.back_off("Instagram showed a login wall")
            return walls + 1
        return None


def refusal(url: str, response: Any) -> SiteError:
    """The error a refused API call raises, for its callers to fall back to the pages.

    A 401 or 403 is a `SiteError` here, not the `LoginError` `site.fetch` would raise: the API
    alone refusing (from the tab too) doesn't stop the pages, which the callers read instead.

    Args:
        url: the call's URL.
        response: its refusal.

    Returns:
        `Blocked` for a 429 still refused after the client's waits; else a `SiteError`.
    """
    message = f"{url}: HTTP {response.status}"
    if response.status == 429:
        return Blocked(f"{message}, rate limited", status=429, response=response)
    hint = LOGIN_HINT if response.status in (401, 403) else ""
    return SiteError(message, status=response.status, response=response, hint=hint)


def throttled(result: Any) -> bool:
    """Whether a response's JSON is Instagram asking to slow down instead of a result."""
    return isinstance(result, dict) and THROTTLED in str(result.get("message", ""))


def _json(response: Any) -> Any:
    """A response's JSON, or None when it isn't JSON.

    Args:
        response: an answer, a refusal's included.

    Returns:
        The parsed body; None for a body that isn't JSON.
    """
    try:
        return response.json
    # A refusal's body is often HTML (a login page): it says nothing a source reads.
    except Changed:
        return None


# ---------------------------------------------------------------------------------------------
# The profile and its tabs
# ---------------------------------------------------------------------------------------------

@dataclass
class Profile:
    """What the profile's pages said so far: its id, picture, highlights, and each tab's posts.

    Args:
        user: the user name.
    """

    # The user name, as the URL gives it.
    user: str
    # The user's id (`pk`), which the story API takes, once a result describing the user came.
    id: str | None = None
    # The largest profile picture, once a result describing the user came.
    picture: str | None = None
    # Whether a result described the user, so the profile page isn't loaded for it.
    described: bool = False
    # Whether the profile page loaded: read once, even when it brought no user result.
    loaded: bool = False
    # The profile's highlights as `(id, title)`, in the order the results list them.
    highlights: list[tuple[str, str]] = field(default_factory=list)
    # The codes each tab listed, so a post a result repeats is yielded once.
    listed: dict[str, set[str]] = field(default_factory=lambda: {tab: set() for tab in TABS})
    # Whether a tab is scrolling now: the tab can't be navigated to a post's page meanwhile.
    scrolling: bool = False

    def read_tab(self, site: Site, api: Api, tab: str) -> Iterator[Post]:
        """Yield the posts of one tab, in the order it lists them, while scrolling it.

        A post that needs its own page (it has no `pk`, or the media API couldn't give it) is
        put aside: opening its page would navigate the tab being scrolled. Those are yielded
        once the scroll ends, when their pages can be opened.

        Args:
            site: the target's site.
            api: the run's `Api`.
            tab: one of `TABS`.

        Yields:
            Each post: whole ones with their files, the rest lazy, the put-aside ones last.

        Raises:
            SiteError: the tab's page didn't load.
            LoginError: Instagram kept showing a login wall, or suspended the account.
        """
        aside: list[Post] = []
        self.scrolling = True
        try:
            for result in self._scroll_tab(site, tab):
                yield from self._posts(site, api, result, tab, aside)
        finally:
            self.scrolling = False
        # The scroll is over: each put-aside post may open its page now. `aside` doesn't grow
        # here, since a files function only puts a post aside while the tab scrolls.
        yield from aside

    def _scroll_tab(self, site: Site, tab: str) -> Iterator[Any]:
        """Each result of one tab: the ones its page embeds, then the ones scrolling brings.

        The scroll ends on the site's word only: the tab's connection says it has no next page
        (`page_state`, which the feed reads too), or the feed ends.

        Args:
            site: the target's site.
            tab: one of `TABS`.

        Yields:
            Each JSON result, in the order the page gave them; throttles are waited out here.

        Raises:
            SiteError: the tab's page didn't load.
        """

        def cursor(result: Any) -> tuple[bool, str | None] | None:
            return page_state(result, tab)

        with site.feed(GRAPHQL, scroll=True, cursor=cursor) as responses:
            if not load(site, f"{SITE}/{self.user}/{TABS[tab]}"):
                # Raised, not returned: a tab that ended here would be recorded complete.
                raise SiteError(f"{tab}: the page didn't load")
            names = list(TABS)
            site.warm([f"{SITE}/{self.user}/{TABS[name]}"
                       for name in names[names.index(tab) + 1:] if not self.listed[name]])
            ended = False
            for result in embedded_results(site.document()):
                yield result
                if (state := page_state(result, tab)) is not None:
                    ended = not state[0]
            # The embedded first page was the last: there is nothing to scroll for.
            if ended:
                return
            for result in responses:
                if throttled(result):
                    site.back_off("Instagram asked to wait a few minutes")
                    continue
                yield result
                state = page_state(result, tab)
                if state is not None and not state[0]:
                    return

    def describe(self, site: Site) -> None:
        """Load the profile page for its id, picture and highlights, once, unless already known.

        2026-11: the page no longer embeds the user or the highlights; its own GraphQL calls bring
        them, so those are read too, once the network is quiet.

        Args:
            site: the target's site.
        """
        if self.described or self.loaded:
            return
        with site.feed(GRAPHQL) as responses:
            if load(site, f"{SITE}/{self.user}/"):
                self.loaded = True
                for result in embedded_results(site.document()):
                    self.note(result)
                for result in responses:
                    self.note(result)

    def note(self, result: Any) -> None:
        """Note the profile's id, picture and highlights from one result.

        Args:
            result: one JSON result of a profile page or tab.
        """
        if not isinstance(result, dict):
            return
        # Only the profile's first result describes its user; later pages may describe others.
        user = dig(result, "data.user")
        if not self.described and isinstance(user, dict):
            self.described = True
            self.picture = profile_picture(result) or self.picture
            found = user.get("pk") or user.get("id")
            self.id = str(found) if found else None
        self.highlights += [h for h in listed_highlights(result) if h not in self.highlights]

    def _posts(self, site: Site, api: Api, result: Any, tab: str,
               aside: list[Post]) -> Iterator[Post]:
        """The new posts of one result: whole ones with their files, the rest lazy or aside.

        Args:
            site: the target's site.
            api: the run's `Api`.
            result: one JSON result of the tab.
            tab: one of `TABS`.
            aside: where a post that needs its page goes, to be yielded after the scroll.

        Yields:
            Each post the result lists that the tab hasn't yet.
        """
        if not isinstance(result, dict):
            return
        self.note(result)
        for media in tab_media(result, tab):
            code = post_name(media)
            if not code or code in self.listed[tab]:
                continue
            self.listed[tab].add(code)
            if is_complete(media):
                if post_files(media):
                    yield post_of(media)
                continue
            pk = media_pk(media)
            lazy = self._lazy(site, api, code, pk, aside)
            # Without a pk only the post's page has it, which can't be opened while scrolling.
            if pk is None:
                aside.append(lazy)
            else:
                yield lazy

    def _lazy(self, site: Site, api: Api, code: str, pk: str | None,
              aside: list[Post]) -> Post:
        """A grid post whose files are read only if the engine wants it, dated by its pk.

        The pk's date lets the engine skip a post by `--when` before any request; the files'
        names still take the post's own `taken_at`.

        Args:
            site: the target's site.
            api: the run's `Api`.
            code: the post's code.
            pk: its id; None when the grid gave none.
            aside: where the post goes when, during the scroll, only its page could give it.

        Returns:
            The lazy post.
        """

        def files() -> list[File]:
            if not self.scrolling:
                return lazy_files(site, api, code, pk) or []
            found = lazy_files(site, api, code, pk, page=False)
            if found is None:
                # Listed again once the scroll ends, when its page can be opened; nothing is
                # saved for it now, so the engine counts it neither saved nor new.
                aside.append(post)
                return []
            return found

        post = Post(code, files, date=pk_stamp(pk), headers=CDN_HEADERS)
        return post


def lazy_files(site: Site, api: Api, code: str, pk: str | None, *,
               page: bool = True) -> list[File] | None:
    """The files of a grid post the grid listed incompletely: from the media API, else its page.

    Args:
        site: the target's site.
        api: the run's `Api`.
        code: the post's code.
        pk: its id, or None when the grid gave none.
        page: whether its page may be opened when the API can't give it.

    Returns:
        Its files, named with the post's own date; [] when it is outside `--when` or its page
        holds nothing; None when only its page could give them and `page` is False.
    """
    media = None
    if pk:
        try:
            media = api.media(site, pk)
        # A page instead of media JSON is the mode `Api.get` announced once: the post's page
        # below is the source, quietly. A refusal is said, as `Api.post` says it.
        except Changed:
            pass
        except (SiteError, NetworkError) as error:
            site.warn("post {code}: media API refused ({error}); reading page",
                      code=code, error=error)
    if media is None:
        if not page:
            return None
        media = post_page(site, code)
    if media is None or media.get("taken_at") not in site.time:
        return []
    # The grid's date came from the pk; the names carry the post's own.
    return files_of(media, pinned=True)


# ---------------------------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------------------------

def load(site: Site, url: str) -> bool:
    """Load an Instagram page, waiting out a login wall or a challenge; False when it didn't load.

        if load(site, "https://www.instagram.com/example_user/"): ...

    Args:
        site: the target's site.
        url: the page's URL.

    Returns:
        Whether it loaded, on the page itself.

    Raises:
        LoginError: the page went to `/accounts/suspended/`, or a login wall stayed after
            `WALL_ATTEMPTS` waits.
    """
    walls = 0
    while True:
        if not site.open(url):
            return False
        where = urlsplit(site.url).path
        if where.startswith(SUSPENDED):
            raise suspended()
        if not is_wall(where):
            return True
        if walls >= WALL_ATTEMPTS:
            raise LoginError("ins: Instagram kept showing a login wall or challenge",
                             hint=f"check it in a browser, or {LOGIN_HINT}")
        walls += 1
        # A throttled session is sent to the login wall or a challenge, which lifts after a wait.
        site.back_off("Instagram showed a login wall")


def is_wall(path: str) -> bool:
    """Whether `path` is a login wall or challenge page."""
    # A whole segment: a profile like `/challengers/` only starts with `/challenge`.
    return any(path == wall or path.startswith(f"{wall}/") for wall in WALLS)


def suspended() -> LoginError:
    """The hard stop for an account Instagram suspended or locked."""
    return LoginError("ins: Instagram suspended or locked this account",
                      hint="check it in a browser")


def embedded_results(page: Any) -> list[Any]:
    """Every GraphQL result a page embeds, each shaped like a response: `{"data": {…}}`.

    Meta's server puts them in `<script type="application/json">` blocks, under `__bbox`.

    Args:
        page: the parsed page (`site.document()`).

    Returns:
        The results, in page order. A block that isn't JSON holds none; the others still count.
    """
    return [result for block in page.blocks("__bbox") for result in _boxed(block)]


def _boxed(node: Any) -> Iterator[Any]:
    """Each `__bbox.result` under `node`, depth first; a box's own result isn't searched again.

    Args:
        node: parsed JSON of one page block, or a part of it.

    Yields:
        Each result, shaped like a GraphQL response.
    """
    if isinstance(node, dict):
        box = node.get("__bbox")
        if isinstance(box, dict) and "result" in box:
            yield box["result"]
            return
        for value in node.values():
            yield from _boxed(value)
    elif isinstance(node, list):
        for value in node:
            yield from _boxed(value)


def post_page(site: Site, code: str) -> dict | None:
    """Open a post's page and return the post it embeds or its GraphQL calls bring.

    Args:
        site: the target's site.
        code: the post's code.

    Returns:
        The post's JSON; None, with a warning, when the page holds no post.
    """
    with site.feed(GRAPHQL) as responses:
        if not load(site, f"{SITE}/p/{code}/"):
            return None
        for result in embedded_results(site.document()):
            if media := post_media(result, code):
                return media
        for result in responses:
            if media := post_media(result, code):
                return media
    site.warn("post {code}: its page holds no post; skipped", code=code)
    return None


def story_posts(site: Site, url: str, where: str) -> Iterator[Post]:
    """Open a story or highlight page and yield a post for each story it lists.

    Args:
        site: the target's site.
        url: the story or highlight page.
        where: the folder the stories go to, for the line counting them.

    Yields:
        One post per story; nothing when the page didn't load.
    """
    if not load(site, url):
        return
    stories = [story for result in embedded_results(site.document())
               for story in story_items(result)]
    yield from reel_posts(site, stories, where)


# ---------------------------------------------------------------------------------------------
# Posts and their files
# ---------------------------------------------------------------------------------------------

def post_of(media: dict) -> Post:
    """A post with its files read: the engine reads `taken_at` as its date."""
    return Post(post_name(media), files_of(media), date=media.get("taken_at"), headers=CDN_HEADERS)


def files_of(media: dict, *, pinned: bool = False) -> list[File]:
    """Each file of a post, named here only where the templates would name it otherwise.

    The templates name a file `<date>_<code>[_<n>]` with the extension its content has. A file
    whose URL has another extension (or none), or every file of a post dated by its pk
    (`pinned`), is named here, from the post's own date, so saved names stay as they were.

    Args:
        media: the post's JSON.
        pinned: name every file here, from the post's `taken_at`.

    Returns:
        The files, in the post's order.
    """
    found = post_files(media)
    code, taken = post_name(media), utc_day(media.get("taken_at"))
    files = []
    for index, (url, extension, audio) in enumerate(found):
        stem = f"{code}_{index}" if len(found) > 1 else code
        first_url = url[0] if isinstance(url, (list, tuple)) else url
        named = pinned or PurePosixPath(urlsplit(first_url).path).suffix.lower() != f".{extension}"
        files.append(File(url, audio=audio, name=file_name(taken, stem, extension) if named
                          else None))
    return files


def reel_posts(site: Site, stories: list[dict], where: str) -> Iterator[Post]:
    """One post per story of a reel, from the API or a story page, and a line counting them.

    Args:
        site: the target's site.
        stories: each story's JSON.
        where: the folder they go to, for the line: "stories", "highlights/<title>".

    Yields:
        One post per story that has a file, keyed by the story's own id.
    """
    for story in stories:
        file = best_file(story)
        if file is None:
            continue
        source, extension, audio = file
        # An `id` carries its owner after an `_`; the story's own id is before it.
        story_id = str(story.get("pk") or story.get("id", "")).split("_")[0]
        taken = utc_day(story.get("taken_at"))
        # The key is the path, so the name's extension must be the one saved files have.
        files = [File(source, audio=audio, name=file_name(taken, story_id, extension))]
        if music := story_music(story):
            files.append(File(music, name=file_name(taken, f"{story_id}_music", "m4a")))
        yield Post(story_id, files, date=story.get("taken_at"), headers=CDN_HEADERS)
    unit = "story" if len(stories) == 1 else "stories"
    site.info("{where}: {n} {unit}", where=where, n=len(stories), unit=unit)


def story_music(story: dict) -> str | None:
    """The URL of a story's music, an M4A Instagram serves whole; None when it has none.

    Args:
        story: one story's JSON, from the reels API or a story page.

    Returns:
        The URL, or None.
    """
    found = dig(story, STORY_MUSIC)
    return found if isinstance(found, str) else None


def file_name(taken: str | None, stem: str, extension: str) -> str:
    """A file's name: `2026-01-02_C1abc_0.jpg`, or `C1abc_0.jpg` for a post without a date.

    Args:
        taken: the post's day as `YYYY-MM-DD`, or None when it has none.
        stem: the name before the extension, such as the code and the file's index.
        extension: the file's extension, without its dot.

    Returns:
        The name.
    """
    return f"{taken}_{stem}.{extension}" if taken else f"{stem}.{extension}"


def utc_day(value: Any) -> str | None:
    """The UTC day of Instagram's `taken_at` (Unix seconds) as `YYYY-MM-DD`; None without one.

    The engine reads the post's date itself; this is only for the names the source sets, which
    must match the day the engine would put in `{date}`.

    Args:
        value: Unix seconds, as a number or digits; anything else has no day.

    Returns:
        The day; None for no value, or one no platform can read as a time.
    """
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC).date().isoformat()
    # Each platform refuses a huge timestamp its own way.
    except (OverflowError, OSError, ValueError):
        return None


def post_name(post: dict) -> str:
    """A post's code, as in its URL, or its number when the result leaves the code out."""
    return str(post.get("code") or post.get("pk") or "")


def media_pk(media: dict) -> str | None:
    """A post's id for the media API: its `pk`, or its `id` before `_<owner>`; None for neither."""
    found = str(media.get("pk") or str(media.get("id") or "").split("_")[0])
    return found if found.isdigit() else None


def pk_stamp(pk: str | None) -> float | None:
    """The Unix time an Instagram id was made at, read from the id itself; None without an id.

        pk_stamp("3141592653589793238")      # 1688727050.013 (2023-07-07 10:50 UTC)

    Only a grid post is dated this way, and only to skip it by `--when` before any request: the
    names take the post's own `taken_at`. The engine refuses a date before 2004 or after next year.

    Args:
        pk: the id, as digits; None or anything else has no time.

    Returns:
        Unix seconds, or None.
    """
    if not pk or not pk.isdigit():
        return None
    return ((int(pk) >> ID_SHIFT) + EPOCH_MS) / 1000


def pk_of_code(code: str) -> str | None:
    """The `pk` a post's code spells in base 64, or None for a code that isn't one.

    Args:
        code: the post's code, as in its link.

    Returns:
        The pk, as digits; None for a code too long (a private post's) or not base64url.
    """
    if not code or len(code) > LONGEST_CODE or any(letter not in CODE_ALPHABET for letter in code):
        return None
    number = 0
    for letter in code:
        number = number * 64 + CODE_ALPHABET.index(letter)
    return str(number)


def picture_name(url: str) -> str:
    """A profile picture's file name, the CDN's own."""
    return urlsplit(url).path.rsplit("/", 1)[-1] or "profile.jpg"


def connection_of(result: Any, tab: str) -> dict | None:
    """The connection where a profile tab's result lists its posts, or None.

        connection = connection_of(result, "reels")

    Args:
        result: one JSON result the tab's page or requests sent.
        tab: one of `TABS`.

    Returns:
        The connection, with its `edges` or `repost_grid_items` and its `page_info`.
    """
    for path in TAB_CONNECTIONS[tab]:
        connection = dig(result, f"data.{path}")
        if isinstance(connection, dict):
            return connection
    return None


def tab_media(result: Any, tab: str) -> list[dict]:
    """The posts one profile tab's result lists: `tab_media(result, "reels")`.

    Args:
        result: one JSON result the tab's page or requests sent.
        tab: one of `TABS`.

    Returns:
        Each listed post's JSON, in order; [] for a result that isn't the tab's.
    """
    connection = connection_of(result, tab)
    if connection is None:
        return []
    if "repost_grid_items" in connection:
        nodes = [item.get("media", item) for item in connection["repost_grid_items"]]
    else:
        nodes = [edge.get("node") or {} for edge in connection.get("edges", [])]
    # The reels tab wraps each post as {"media": post}.
    return [node["media"] if isinstance(node.get("media"), dict) else node for node in nodes]


def page_state(result: Any, tab: str) -> tuple[bool, str | None] | None:
    """Whether a tab's result says there are more pages, and its cursor; None when it says nothing.

    A result that isn't the tab's, or whose connection has no `page_info`, says nothing: the
    scroll then goes on.

    Args:
        result: one JSON result the tab's page or requests sent.
        tab: one of `TABS`.

    Returns:
        `(more, cursor)`, as `site.feed`'s `cursor` takes it; None when the result says nothing.
    """
    connection = connection_of(result, tab) if isinstance(result, dict) else None
    info = connection.get("page_info") if connection is not None else None
    if not isinstance(info, dict):
        return None
    cursor = info.get("end_cursor")
    return bool(info.get("has_next_page") and cursor), str(cursor) if cursor else None


def is_complete(media: dict) -> bool:
    """Whether a grid item is the whole post, to be saved as listed.

    Args:
        media: the grid item's JSON.

    Returns:
        True for a dated feed photo, carousel or reel whose every file is at full size.
    """
    if not media.get("taken_at"):
        return False
    kind = media.get("product_type")
    if kind == "carousel_container":
        children = media.get("carousel_media")
        return bool(children) and all(full_size(child) for child in children)
    if kind == "clips":
        return bool(media.get("video_versions") or media.get("video_dash_manifest"))
    if kind == "feed":
        return all(full_size(child) for child in media.get("carousel_media") or [media])
    return False


def full_size(item: dict) -> bool:
    """Whether a listed item has its whole file: a video's paths, or an image near full width.

    Args:
        item: a post's or carousel item's JSON.

    Returns:
        True when its largest image is within `FULL_SIZE` of `original_width`, or it is a
        video with its paths.
    """
    if item.get("media_type") == 2 or item.get("video_versions"):
        return bool(item.get("video_versions") or item.get("video_dash_manifest"))
    images = _candidates(item)
    original = item.get("original_width")
    if not images or not isinstance(original, (int, float)) or original <= 0:
        return False
    return (max(images, key=_area).get("width") or 0) >= FULL_SIZE * original


def post_media(result: Any, code: str | None = None) -> dict | None:
    """The post a post's own page shows, or None when this result isn't it.

    Args:
        result: one JSON result of the post's page.
        code: the post's code, which a result naming another code isn't; None takes any.

    Returns:
        The post's JSON, or None.
    """
    media = dig(result, POST)
    if not isinstance(media, dict):
        return None
    if code is not None and media.get("code") and media.get("code") != code:
        return None
    return media


def story_items(result: Any) -> list[dict]:
    """The stories of a story or highlight page's result."""
    return [item for item in dig(result, STORIES) or [] if isinstance(item, dict)]


def listed_highlights(result: Any) -> list[tuple[str, str]]:
    """The profile's highlights as `(id, title)`, from the result that lists them."""
    nodes = dig(result, "data.highlights.edges[].node") or []
    return [(str(node["id"]).removeprefix("highlight:"), node.get("title") or "highlight")
            for node in nodes if isinstance(node, dict) and node.get("id")]


def profile_picture(result: Any) -> str | None:
    """The largest profile picture of the profile's user, from the result that describes them."""
    found = dig(result, "data.user.hd_profile_pic_url_info.url")
    return found if isinstance(found, str) else None


def post_files(media: dict) -> list[tuple[str | tuple[str, ...], str, str | None]]:
    """Each file of a post as `(url, extension, audio)`: every carousel item, or the post itself."""
    children = media.get("carousel_media") or [media]
    return [file for child in children if (file := best_file(child)) is not None]


def best_file(media: dict) -> tuple[str | tuple[str, ...], str, str | None] | None:
    """An item's best file as `(url, extension, audio)`: its DASH streams, its MP4, or its image.

    If both DASH streams and direct progressive video versions are present, the progressive
    URL is kept as a fallback mirror in case DASH merge fails on video.

    Args:
        media: one post's or carousel item's JSON.

    Returns:
        The file; None for an item without any.
    """
    videos = media.get("video_versions") or []
    progressive = max(videos, key=_area)["url"] if videos else None
    if streams := File.dash(media.get("video_dash_manifest")):
        # The joined file takes the video's extension: WebM for a WebM stream, else MP4.
        video = str(streams.url)
        webm = PurePosixPath(urlsplit(video).path).suffix.lower() == ".webm"
        urls = (video, progressive) if progressive and progressive != video else video
        return urls, "webm" if webm else "mp4", streams.audio
    if progressive:
        return progressive, "mp4", None
    images = _candidates(media)
    if images:
        url = max(images, key=_area)["url"]
        return url, image_extension(url), None
    return None


def image_extension(url: str) -> str:
    """An image's extension from its URL: older posts are served as WebP, newer ones as JPEG."""
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower().lstrip(".")
    return suffix if suffix in IMAGE_EXTENSIONS else "jpg"


def _candidates(item: dict) -> list[dict]:
    """An item's image sizes, each with its `url`, `width` and `height`."""
    found = dig(item, "image_versions2.candidates")
    return [image for image in found if isinstance(image, dict)] if isinstance(found, list) else []


def _area(version: dict) -> int:
    return (version.get("width") or 0) * (version.get("height") or 0)

