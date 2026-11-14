"""Instagram: posts, reels, tagged posts, reposts, stories, highlights, and profile pictures.

Reads data with saved cookies, using REST API endpoints first and page scraping as fallback.
A profile tab streams complete posts while scrolling to the end, then resolves incomplete grid
items via API or post pages.

Stories and highlights are fetched via the reels API, falling back to story pages.
Rate limits and challenges are handled with back_off.

Folder layout under `.flnd/media/ins/<username>/`:
  posts/, reels/, tagged/, reposts/   <date>_<code>[_<idx>].jpg (or .mp4)
  stories/, highlights/<title>/       <date>_<id>.jpg (or .mp4)
  profile/                            profile_<id>.jpg
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Iterator, Mapping
from urllib.parse import urlsplit

from flnd.source import FetchError, File, LoginError, Post, Site, Source, day, target
from flnd.sources.common import dash
from flnd.sources.common.relay import embedded_results
from flnd.sources.common.watch import caught_up, warm_tabs, watch_responses
from shared import fmt, http, selector

if TYPE_CHECKING:
    import httpx

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
# Web client app id and ASBD id sent on API calls.
APP_ID = "936619743392459"
ASBD_ID = "129477"
# Where Instagram sends a session it stopped: a suspended or locked account (a hard stop), and the
# login wall or challenge it puts up while it throttles (waited out).
SUSPENDED = "/accounts/suspended"
WALLS = ("/accounts/login", "/challenge")
# Consecutive posts older than `since` before stopping scroll early (more than 3 pinned posts).
OLD_STREAK = 5
# Seconds a refused API is left alone: Instagram's refusals last, and asking again each run only
# costs a request and its retries before the story pages are read anyway.
REFUSAL_LIFETIME = 24 * 60 * 60
# Reels asked for in one `reels_media` call: more is refused (gallery-dl's chunk size).
REELS_PER_CALL = 5
# A grid image within this share of the post's `original_width` is its full size, not a thumbnail.
FULL_SIZE = 0.9
# Instagram ids carry creation time: milliseconds since this moment (2011-08-24 21:07:01.721
# UTC), above the lowest 23 bits (the shard and a sequence number).
EPOCH_MS = 1314220021721
ID_SHIFT = 23
# A post's code is its `pk` in base 64, in this alphabet; a longer code (a private post's) isn't.
CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
LONGEST_CODE = 11
# Statuses indicating Instagram wants browser-origin calls; tab sends them.
REFUSED_DIRECT = frozenset({400, 401, 403})
# What a GraphQL call's URL holds: the responses that carry a profile tab's or a post's data.
GRAPHQL = "graphql"
# Where a story's music is, in the two shapes Instagram sends it (as gallery-dl reads it): a music
# sticker's asset, or the story's own music metadata.
STORY_MUSIC = ("story_music_stickers[0].music_asset_info.progressive_download_url || "
               "music_metadata.music_info.music_asset_info.progressive_download_url")
CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
WALL_ATTEMPTS = 2
CHECKPOINTS = ("checkpoint_required", "challenge_required", "login_required")


class Instagram(Source):
    """Instagram profiles and single posts: the API first, the browser's pages as the fallback."""

    code = "ins"
    name = "Instagram"
    domains = ("instagram.com",)
    needs = ("sessionid",)
    user_agent = CHROME
    browser = True
    # Instaloader's measured budgets set the ceiling; the floor only spaces a burst while a window
    # still has room.
    pace = http.Pace(0.2, budgets={"/graphql": (275, 600), "/api/v1/": (75, 660)})
    # Post category or highlight title: "posts", "highlights/<title>", or "".
    folder = "{category}/{album}"
    # Every story link, a user's or a highlight's: a story expires, but the profile lists them all.
    unsupported = {
        "instagram.com/stories": (
            "story links aren't supported; give the profile, and its stories come with it"
        ),
    }

    def __init__(self) -> None:
        # How this run reaches the API; one source instance serves the whole run.
        self.api = Api()

    def prepare(self, request: httpx.Request, cookies: Mapping[str, str], /) -> httpx.Request:
        """Instagram web API headers for requests sent to instagram.com."""
        request.headers.update(self.api.headers(cookies))
        return request

    # `post` comes before `user`, so instagram.com/p/<code> is never the user "p". A post's code is
    # base64url, so it takes `-` as well as `\w`.
    @target(
        r"instagram.com/p/{code:[\w-]+}", r"instagram.com/reel/{code:[\w-]+}",
        r"instagram.com/reels/{code:[\w-]+}", r"instagram.com/tv/{code:[\w-]+}",
        r"instagram.com/*/p/{code:[\w-]+}", r"instagram.com/*/reel/{code:[\w-]+}",
        r"instagram.com/*/reels/{code:[\w-]+}", r"instagram.com/*/tv/{code:[\w-]+}",
    )
    def post(self, site: Site, code: str) -> Iterator[Post]:
        """One post or reel by its code."""
        media = self.api.post(site, code)
        if media is not None:
            yield post_of(media)

    @target("{user}", "instagram.com/{user}")
    def user(self, site: Site, user: str) -> Profile:
        """A profile, by its user name; a tab's URL (/reels/, /tagged/) is its profile too.

        Nothing is loaded yet: a tab, or the first category that needs the profile's page, loads it.

            profile = source.user(site, "example_user")

        Args:
            site: the target's site.
            user: the user name, as the URL gives it.
        """
        return Profile(user)

    @user.category(newest_first=True)
    def posts(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile's posts, newest first, to the end of the grid.

            collect(Instagram, "ins:example_user", options=Options(categories=("posts",)))

        Args:
            site: the target's site.
            profile: the user's `Profile`, which its tabs fill in as they are read.
        """
        yield from profile.read_tab(site, self.api, "posts")

    @user.category(newest_first=True)
    def reels(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile's reels, newest first."""
        yield from profile.read_tab(site, self.api, "reels")

    @user.category
    def tagged(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The posts the profile is tagged in."""
        yield from profile.read_tab(site, self.api, "tagged")

    @user.category
    def reposts(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The posts the profile reposted."""
        yield from profile.read_tab(site, self.api, "reposts")

    @user.category(key="")
    def stories(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile's active stories from story API or page; keyed by path."""
        profile.describe(site)
        stories = self.api.stories(site, profile.id) if profile.id else None
        if stories is None:
            yield from story_posts(site, f"{SITE}/stories/{profile.user}/", "stories")
            return
        yield from reel_posts(site, stories, "stories")

    @user.category(key="")
    def highlights(self, site: Site, profile: Profile) -> Iterator[Post]:
        """Each highlight's stories in its folder; keyed by path."""
        profile.describe(site)
        found = profile.highlights
        for start in range(0, len(found), REELS_PER_CALL):
            chunk = found[start:start + REELS_PER_CALL]
            ids = [f"highlight:{hid}" for hid, _ in chunk]
            reels = self.api.reels(site, ids, "highlights")
            if reels is None:
                warm_tabs(site, [f"{SITE}/stories/highlights/{hid}/" for hid, _ in chunk])
            for highlight_id, title in chunk:
                folder = f"highlights/{fmt.filename(title)}"
                if reels is None:
                    posts = story_posts(site, f"{SITE}/stories/highlights/{highlight_id}/", folder)
                else:
                    posts = reel_posts(site, reels.get(f"highlight:{highlight_id}", []), folder)
                for post in posts:
                    yield post.replace(album=title)

    @user.category(folder="profile", key="")
    def profile(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile picture; keyed by path."""
        profile.describe(site)
        if profile.picture:
            pic = File(profile.picture, name=picture_name(profile.picture))
            yield Post(profile.user, [pic], headers=CDN_HEADERS)


class Api:
    """How one run reaches Instagram REST API: headers, claims, tab transport.

        media = Api().media(site, "3141592653589793238")      # a post, or None when it isn't there

    Every call waits out a throttle (`site.back_off`) and calls again, as long as it takes, and
    stops hard on `/accounts/suspended/`. The first refusal of flnd's own client (a 400, 401 or
    403, or a login wall) moves every later call into the logged-in tab, with one warning.
    """

    def __init__(self) -> None:
        # The `X-IG-WWW-Claim` Instagram gave last, echoed on next calls.
        self.claim = "0"
        # Whether the tab sends calls (`fetch(in_page=True)`) after client refusal.
        self.in_page = False
        # The APIs Instagram refused this run, so a refusal is warned of once and never asked again.
        self.refused: set[str] = set()

    def headers(self, cookies: Mapping[str, str]) -> dict[str, str]:
        """The headers Instagram's web client sends its API."""
        sent = {"X-IG-App-ID": APP_ID, "X-ASBD-ID": ASBD_ID, "X-IG-WWW-Claim": self.claim,
                "X-Requested-With": "XMLHttpRequest", "Referer": f"{SITE}/"}
        if cookies.get("csrftoken"):
            sent["X-CSRFToken"] = cookies["csrftoken"]
        return sent

    def get(self, site: Site, url: str, *, params: Any = None) -> Any | None:
        """One API call's JSON, every throttle waited out; None for a 404.

            data = api.get(site, f"{API}/media/{pk}/info/")

        Args:
            site: the target's site.
            url: the API URL.
            params: query params as dict or pairs.

        Raises:
            FetchError: refusal from tab call or unparseable answer.
                answer that isn't JSON. The caller falls back to the page.
            LoginError: Instagram sent the call to `/accounts/suspended/`.
        """
        # Each pass makes one call; throttles and login walls are waited out.
        walls = 0
        while True:
            try:
                response = site.fetch(url, params=params, in_page=self.in_page,
                                      headers=self.headers(site.cookies) if self.in_page else None)
            # A 404 is a post or user that isn't there, which is an answer, not an error. Any other
            # refusal goes to `_waited`, which may move the calls into the tab.
            except FetchError as error:
                if error.status == 404:
                    return None
                next_walls = self._waited(
                    site, error.response, refused=error.status in REFUSED_DIRECT, walls=walls,
                )
                if next_walls is not None:
                    walls = next_walls
                    continue
                raise
            next_walls = self._waited(site, response, refused=False, walls=walls)
            if next_walls is not None:
                walls = next_walls
                continue
            # Instagram rotates this claim and expects it echoed back on next calls.
            self.claim = response.headers.get("x-ig-set-www-claim") or self.claim
            try:
                return response.json
            # Non-JSON answers are HTML error or login pages; caller reads page.
            except http.Error:
                raise FetchError(f"{url}: Instagram's answer isn't JSON") from None

    def media(self, site: Site, pk: str) -> dict | None:
        """One post by `pk` from `/api/v1/media/<pk>/info/`, or None.

            media = api.media(site, "3141592653589793238")

        Args:
            site: the target's site.
            pk: the post's id, as digits.

        Raises:
            FetchError, LoginError: as `get`.
        """
        if "media" in self.refused:
            return None
        try:
            data = self.get(site, f"{API}/media/{pk}/info/")
        except FetchError:
            if self.in_page:
                self.refused.add("media")
            raise
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        return None

    def post(self, site: Site, code: str) -> dict | None:
        """One post by code: from media API by pk, else from page, or None.

            media = api.post(site, "C1abc")

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
            except FetchError as error:
                site.warn(
                    "post {code}: media API refused ({error}); reading page",
                    code=code, error=error,
                )
        # A code that decodes to another post (a private post's longer code) is never taken for it.
        if media is not None and media.get("code") == code:
            return media
        return post_page(site, code)

    def reels(self, site: Site, reel_ids: list[Any], what: str) -> dict[str, list[dict]] | None:
        """Stories of up to 5 reels from `/api/v1/feed/reels_media/`."""
        if self._refused(site, "reels"):
            return None
        try:
            params = [("reel_ids", str(reel)) for reel in reel_ids]
            data = self.get(site, f"{API}/feed/reels_media/", params=params)
        except FetchError:
            self._refuse(site, "reels", what)
            return None
        # An answer without the reels' JSON is a refusal too, so the same fallback applies.
        if not isinstance(data, dict):
            self._refuse(site, "reels", what)
            return None
        found: dict[str, list[dict]] = {}
        for reel in data.get("reels_media") or list((data.get("reels") or {}).values()):
            if isinstance(reel, dict):
                items = [item for item in reel.get("items") or [] if isinstance(item, dict)]
                found[str(reel.get("id", ""))] = items
        return found

    def stories(self, site: Site, user_id: Any) -> list[dict] | None:
        """One user's active stories from `/api/v1/feed/user/<id>/story/`."""
        if self._refused(site, "story"):
            return None
        try:
            data = self.get(site, f"{API}/feed/user/{user_id}/story/")
        except FetchError:
            self._refuse(site, "story", "stories")
            return None
        # `reel` is null while user has no story; an answer without `reel` isn't this API's.
        if not isinstance(data, dict) or "reel" not in data:
            self._refuse(site, "story", "stories")
            return None
        if not isinstance(data["reel"], (dict, type(None))):
            self._refuse(site, "story", "stories")
            return None
        return [item for item in (data["reel"] or {}).get("items") or [] if isinstance(item, dict)]

    def _refused(self, site: Site, api: str) -> bool:
        """Whether `api` was refused this run, or in an earlier run less than a day ago."""
        if api in self.refused:
            return True
        since = site.state.get(f"refused.{api}")
        if isinstance(since, (int, float)) and time.time() - since < REFUSAL_LIFETIME:
            self.refused.add(api)
            return True
        return False

    def _refuse(self, site: Site, api: str, what: str) -> None:
        """Leave `api` alone for a day, and say once that `what` is read from the story pages."""
        self.refused.add(api)
        site.state[f"refused.{api}"] = time.time()
        # Expected while Instagram refuses these APIs, so an info line, not a warning.
        site.info(
            "{what}: reading the story pages, as Instagram's {api} API is refusing",
            what=what, api=api,
        )

    def _waited(
        self, site: Site, response: http.Response | None, *, refused: bool, walls: int,
    ) -> int | None:
        """Next wall count if waited or moved to tab, else None."""
        # The suspended page is the hard stop: nothing is retried past it.
        where = urlsplit(response.url).path if response is not None else ""
        if where.startswith(SUSPENDED):
            raise suspended()
        data = _json(response)
        msg = str(data.get("message", "")).lower() if isinstance(data, dict) else ""
        has_login = isinstance(data, dict) and data.get("require_login")
        has_checkpoint = any(key in msg for key in CHECKPOINTS)
        walled = any(where.startswith(wall) for wall in WALLS) or has_login or has_checkpoint
        if (refused or walled) and not self.in_page and not throttled(data):
            # Instagram may refuse httpx TLS fingerprint; tab fetch has Chrome's.
            self.in_page = True
            site.warn("Instagram refused flnd's own client; sending its API calls from the page")
            return walls
        if throttled(data):
            site.back_off("Instagram asked to wait a few minutes")
            return walls
        if walled:
            if walls >= WALL_ATTEMPTS:
                who = response.url if response is not None else SITE
                raise FetchError(f"{who}: Instagram showed a login wall")
            site.back_off("Instagram showed a login wall")
            return walls + 1
        return None


@dataclass
class Profile:
    """Profile results collected so far: id, picture, highlights, posts.

    Args:
        user: the user name.
    """

    # The user name, as the URL gives it.
    user: str
    # The user's id (`pk`), which the reels API takes, once a result describing the user came.
    id: str | None = None
    # The largest profile picture, once a result describing the user came.
    picture: str | None = None
    # Whether user result arrived, avoiding redundant page loads.
    described: bool = False
    # The profile's highlights as `(id, title)`, in the order the results list them.
    highlights: list[tuple[str, str]] = field(default_factory=list)
    # The codes each tab listed, so a post a result repeats is yielded once.
    listed: dict[str, set[str]] = field(default_factory=lambda: {tab: set() for tab in TABS})

    def read_tab(self, site: Site, api: Api, tab: str) -> Iterator[Post]:
        """Stream complete posts while scrolling `tab`, then yield deferred lazy posts."""
        deferred: list[Post] = []
        for result in self._scroll_tab(site, tab):
            yield from self._posts(site, api, result, tab, deferred=deferred)
        yield from deferred

    def _scroll_tab(self, site: Site, tab: str) -> Iterator[Any]:
        """Scroll one profile tab until it ends or hits `OLD_STREAK`, yielding results live."""
        since = site.time.since if tab in ("posts", "reels") else None
        ended, old = False, 0
        with watch_responses(site, GRAPHQL, scroll=True) as responses:
            if not load(site, f"{SITE}/{self.user}/{TABS[tab]}"):
                # Raised, not returned: a tab that ended here would be recorded complete.
                raise FetchError(f"{tab}: the page didn't load")
            keys = list(TABS)
            warm_tabs(
                site,
                [
                    f"{SITE}/{self.user}/{TABS[name]}"
                    for name in keys[keys.index(tab) + 1:]
                    if not self.listed[name]
                ],
            )
            for result in embedded_results(site.page.document):
                yield result
                old = older_streak(result, tab, since, old)
                if (state := page_state(result, tab)) is not None:
                    ended = not state[0]
                if _hit_saved(result, tab):
                    return
            if ended or old >= OLD_STREAK:
                return
            for result in responses:
                if throttled(result):
                    site.back_off("Instagram asked to wait a few minutes")
                    continue
                yield result
                old = older_streak(result, tab, since, old)
                state = page_state(result, tab)
                no_more = state is not None and not state[0]
                if no_more or old >= OLD_STREAK or _hit_saved(result, tab):
                    return

    def describe(self, site: Site) -> None:
        """Load profile page for id, picture and highlights if not yet known.

        2026-11: the page no longer embeds the user or the highlights; its own GraphQL calls bring
        them, so those are read too, once the network is quiet.
        """
        if self.described:
            return
        with watch_responses(site, GRAPHQL) as responses:
            if load(site, f"{SITE}/{self.user}/"):
                for result in embedded_results(site.page.document):
                    self.note(result)
                for result in responses:
                    self.note(result)

    def note(self, result: Any) -> None:
        """Note the profile's id, picture and highlights from one result."""
        if not isinstance(result, dict):
            return
        # Only the profile's first result describes its user; later pages may describe others.
        user = selector.parse(result).jmespath("data.user").first
        picture = profile_picture(result)
        if not self.described and isinstance(user, dict):
            self.described = True
            if picture:
                self.picture = picture
            found = user.get("pk") or user.get("id")
            self.id = str(found) if found else None
        new_hl = [h for h in listed_highlights(result) if h not in self.highlights]
        self.highlights += new_hl

    def _posts(
        self, site: Site, api: Api, result: Any, tab: str, *, deferred: list[Post] | None = None,
    ) -> Iterator[Post]:
        """Yield complete posts live and append incomplete ones to `deferred` when given."""
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
            # Dated by pk so engine skips by `--time` before request, and by key when saved.
            fetch_files = lambda code=code, pk=pk: lazy_files(site, api, code, pk)
            lazy = Post(code, fetch_files, date=pk_stamp(pk), headers=CDN_HEADERS)
            if deferred is None:
                yield lazy
            else:
                deferred.append(lazy)


def load(site: Site, url: str) -> bool:
    """Load an Instagram page, waiting out a login wall or a challenge; False when it didn't load.

        if load(site, "https://www.instagram.com/example_user/"):

    Args:
        site: the target's site.
        url: the page's URL.

    Raises:
        LoginError: the page went to `/accounts/suspended/`.
    """
    walls = 0
    while True:
        if not site.goto(url):
            return False
        where = urlsplit(site.page.url).path
        if where.startswith(SUSPENDED):
            raise suspended()
        if not any(where.startswith(wall) for wall in WALLS):
            return True
        if walls >= WALL_ATTEMPTS:
            raise LoginError(
                "ins: Instagram kept showing a login wall or challenge; "
                "check it in a browser or run flnd auth login ins"
            )
        walls += 1
        # Throttled session is sent to login wall or challenge; lifts after wait.
        site.back_off("Instagram showed a login wall")


def suspended() -> LoginError:
    """The hard stop for an account Instagram suspended or locked."""
    return LoginError("ins: Instagram suspended or locked this account; check it in a browser")


def lazy_files(site: Site, api: Api, code: str, pk: str | None) -> list[File]:
    """Files of incomplete grid post from media API or page."""
    media = None
    if pk:
        try:
            media = api.media(site, pk)
        # A refused media API falls back to the post's page, as `Api.post` does.
        except FetchError as error:
            site.warn(
                    "post {code}: media API refused ({error}); reading page",
                    code=code, error=error,
                )
    if media is None:
        media = post_page(site, code)
    if media is None or day(media.get("taken_at")) not in site.time:
        return []
    # The grid's date came from the pk; the names carry the post's own, as they always did.
    return files_of(media, pinned=True)


def throttled(result: Any) -> bool:
    """Whether a response's JSON is Instagram asking to slow down instead of a result."""
    return isinstance(result, dict) and THROTTLED in str(result.get("message", ""))


def _json(response: http.Response | None) -> Any:
    """A response's JSON, or None when there is no response or it isn't JSON."""
    if response is None:
        return None
    try:
        return response.json
    # A refusal's body is often HTML (a login page): it says nothing a source reads.
    except http.Error:
        return None


def post_page(site: Site, code: str) -> dict | None:
    """Open post page and return embedded or GraphQL post, or None with warning."""
    with watch_responses(site, GRAPHQL) as responses:
        if not load(site, f"{SITE}/p/{code}/"):
            return None
        for result in embedded_results(site.page.document):
            if media := post_media(result, code):
                return media
        for result in responses:
            if media := post_media(result, code):
                return media
    site.warn("post {code}: its page holds no post; skipped", code=code)
    return None


def post_of(media: dict) -> Post:
    """A post with its date and files read: the engine reads `taken_at` as the date."""
    return Post(post_name(media), files_of(media), date=media.get("taken_at"), headers=CDN_HEADERS)


def files_of(media: dict, *, pinned: bool = False) -> list[File]:
    """Each file of a post, named `<date>_<code>[_<n>].<ext>`."""
    found = post_files(media)
    code, taken = post_name(media), day(media.get("taken_at"))
    files = []
    for index, (url, extension, audio) in enumerate(found):
        stem = f"{code}_{index}" if len(found) > 1 else code
        named = pinned or PurePosixPath(urlsplit(url).path).suffix.lower() != f".{extension}"
        fname = file_name(taken, stem, extension) if named else None
        files.append(File(url, audio=audio, name=fname))
    return files


def story_posts(site: Site, url: str, folder: str) -> Iterator[Post]:
    """Open story or highlight page and yield post for each story."""
    if not load(site, url):
        return
    stories = [
        story for result in embedded_results(site.page.document)
        for story in story_items(result)
    ]
    yield from reel_posts(site, stories, folder)


def reel_posts(site: Site, stories: list[dict], folder: str) -> Iterator[Post]:
    """One post for each story of a reel from API or story page."""
    for story in stories:
        file = best_file(story)
        if file is None:
            continue
        source, extension, audio = file
        # An `id` carries its owner after an `_`; the story's own id is before it.
        story_id = str(story.get("pk") or story.get("id", "")).split("_")[0]
        # Key is path, so filename extension must match saved format.
        name = file_name(day(story.get("taken_at")), story_id, extension)
        files = [File(source, audio=audio, name=name)]
        if music := story_music(story):
            mname = file_name(day(story.get("taken_at")), f"{story_id}_music", "m4a")
            files.append(File(music, name=mname))
        yield Post(story_id, files, date=story.get("taken_at"), headers=CDN_HEADERS)
    cnt = fmt.count(len(stories), "story", plural="stories")
    site.info("{folder}: {stories}", folder=folder, stories=cnt)


def story_music(story: dict) -> str | None:
    """The URL of a story's music, an M4A Instagram serves whole; None when it has none.

    Args:
        story: one story's JSON, from the reels API or a story page.
    """
    return selector.parse(story).jmespath(STORY_MUSIC).first


def file_name(taken: Any, stem: str, extension: str) -> str:
    """A file's name: `2026-01-02_C1abc_0.jpg`, or `C1abc_0.jpg` for a post without a date.

        file_name("2026-01-02", "C1abc_0", "jpg")

    Args:
        taken: the post's day as `YYYY-MM-DD`, or None when it has none.
        stem: the name before the extension, such as the code and the file's index.
        extension: the file's extension, without its dot.
    """
    return f"{taken}_{stem}.{extension}" if taken else f"{stem}.{extension}"


def post_name(post: dict) -> str:
    """A post's code, as in its URL, or its number when the result leaves the code out."""
    return str(post.get("code") or post.get("pk") or "")


def media_pk(media: dict) -> str | None:
    """Post id for media API: `pk` or `id` before `_<owner>`, or None."""
    found = str(media.get("pk") or str(media.get("id") or "").split("_")[0])
    return found if found.isdigit() else None


def pk_stamp(pk: str | None) -> float | None:
    """The Unix time an Instagram id was made at, read from the id itself; None without an id.

        pk_stamp("3141592653589793238")      # 1688727050.013 (2023-07-07 10:50 UTC)

    Only a grid post is dated this way, and only to skip it by `--time` before any request: the
    names take the post's own `taken_at`. The engine refuses a date before 2004 or after next year.
    """
    if not pk or not pk.isdigit():
        return None
    return ((int(pk) >> ID_SHIFT) + EPOCH_MS) / 1000


def pk_of_code(code: str) -> str | None:
    """The `pk` a post code spells in base 64, or None."""
    if not code or len(code) > LONGEST_CODE or any(letter not in CODE_ALPHABET for letter in code):
        return None
    number = 0
    for letter in code:
        number = number * 64 + CODE_ALPHABET.index(letter)
    return str(number)


def picture_name(url: str) -> str:
    """Profile picture filename from Instagram URL."""
    return urlsplit(url).path.rsplit("/", 1)[-1] or "profile.jpg"


def connection_of(result: Any, tab: str) -> dict | None:
    """Connection where profile tab lists posts, or None.

        connection = connection_of(result, "reels")

    Args:
        result: one JSON result the tab's page or requests sent.
        tab: one of `TABS`.
    """
    found = selector.parse(result)
    for path in TAB_CONNECTIONS[tab]:
        connection = found.jmespath(f"data.{path}").first
        if isinstance(connection, dict):
            return connection
    return None


def tab_media(result: Any, tab: str) -> list[dict]:
    """The posts one profile tab's result lists: `tab_media(result, "reels")`.

    Args:
        result: one JSON result the tab's page or requests sent.
        tab: one of `TABS`.
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
    """Whether tab result has more pages and cursor, or None.

    A result that isn't the tab's, or whose connection has no `page_info`, says nothing: the
    scroll then goes on.

    Args:
        result: one JSON result the tab's page or requests sent.
        tab: one of `TABS`.
    """
    connection = connection_of(result, tab) if isinstance(result, dict) else None
    info = connection.get("page_info") if connection is not None else None
    if not isinstance(info, dict):
        return None
    cursor = info.get("end_cursor")
    return bool(info.get("has_next_page") and cursor), str(cursor) if cursor else None


def _hit_saved(result: Any, tab: str) -> bool:
    """Whether a `newest_first` tab result hits the 5-saved-post early-stop streak."""
    if tab not in ("posts", "reels") or not isinstance(result, dict):
        return False
    for media in tab_media(result, tab):
        code = post_name(media)
        posted = media.get("taken_at") or pk_stamp(media_pk(media))
        if code and caught_up(code, posted=posted):
            return True
    return False


def older_streak(result: Any, tab: str, since: date | None, streak: int) -> int:
    """Count consecutive posts in `result` older than `since`, carrying `streak` forward."""
    if since is None or not isinstance(result, dict):
        return streak
    for media in tab_media(result, tab):
        posted = day(media.get("taken_at") or pk_stamp(media_pk(media)))
        if posted is None:
            continue
        streak = streak + 1 if posted < since else 0
    return streak


def is_complete(media: dict) -> bool:
    """Whether a grid item is the whole post, to be saved as listed."""
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
    """Whether listed item has full file (video or >=90% width image)."""
    if item.get("media_type") == 2 or item.get("video_versions"):
        return bool(item.get("video_versions") or item.get("video_dash_manifest"))
    images = selector.parse(item).jmespath("image_versions2.candidates").all
    original = item.get("original_width")
    if not images or not isinstance(original, (int, float)) or original <= 0:
        return False
    return (max(images, key=_area).get("width") or 0) >= FULL_SIZE * original


def post_media(result: Any, code: str | None = None) -> dict | None:
    """The post a post's own page shows, or None when this result isn't it."""
    media = selector.parse(result).jmespath(POST).first
    if not isinstance(media, dict):
        return None
    if code is not None and media.get("code") and media.get("code") != code:
        return None
    return media


def story_items(result: Any) -> list[dict]:
    """The stories of a story or highlight page."""
    return [item for item in selector.parse(result).jmespath(STORIES).all if isinstance(item, dict)]


def listed_highlights(result: Any) -> list[tuple[str, str]]:
    """The profile's highlights as `(id, title)`, from the result that lists them."""
    nodes = selector.parse(result).jmespath("data.highlights.edges[].node").all
    return [(str(node["id"]).removeprefix("highlight:"), node.get("title") or "highlight")
            for node in nodes if isinstance(node, dict) and node.get("id")]


def profile_picture(result: Any) -> str | None:
    """The largest profile picture of the profile's user, from the result that describes them."""
    return selector.parse(result).jmespath("data.user.hd_profile_pic_url_info.url").first


def post_files(media: dict) -> list[tuple[str, str, str | None]]:
    """Each file of a post as `(url, extension, audio)`: every carousel item, or the post itself."""
    children = media.get("carousel_media") or [media]
    return [file for child in children if (file := best_file(child)) is not None]


def best_file(media: dict) -> tuple[str, str, str | None] | None:
    """Item best file as (url, extension, audio): DASH, progressive, or image."""
    if streams := dash.best(media.get("video_dash_manifest")):
        return streams.video, streams.extension[1:], streams.audio
    videos = media.get("video_versions") or []
    if videos:
        return max(videos, key=_area)["url"], "mp4", None
    images = selector.parse(media).jmespath("image_versions2.candidates").all
    if images:
        url = max(images, key=_area)["url"]
        return url, image_extension(url), None
    return None


def image_extension(url: str) -> str:
    """An image's extension from its URL: older posts are served as WebP, newer ones as JPEG."""
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower().lstrip(".")
    return suffix if suffix in IMAGE_EXTENSIONS else "jpg"


def _area(version: dict) -> int:
    return (version.get("width") or 0) * (version.get("height") or 0)
