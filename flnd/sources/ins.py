"""Instagram: a profile's posts, reels, tagged posts, reposts, stories, highlights and picture, or one post.

Everything is read with the saved cookies, API first and the page as the fallback (plan/phase-6/ins.md):
  1. A profile tab is loaded once in the browser, for the first page of posts its HTML embeds. It is
     scrolled only until the page sends its own next-page GraphQL request, which is kept and from
     then on sent again from the tab with each new `after` cursor (`Replay`): one small JSON
     request a page, with the `doc_id`, `fb_dtsg`, `lsd` and relay variables the page uses today.
     A replay the site refuses (not a rate limit) warns once and the tab is scrolled to its end.
  2. A grid item is saved as the grid lists it only when it really is whole: its `product_type` is
     "feed", it has its date, and every image in it is within 10% of its `original_width` (gallery-dl's
     rule). Anything else (a reel, a carousel, a thumbnail) becomes a lazy post, dated by its `pk`, whose
     files come from `/api/v1/media/<pk>/info/` only once the engine knows the post is new and in
     `--time`. A post with no `pk` is read from its own page instead.
  3. Stories and highlights come from `/api/v1/feed/reels_media/`, at most 5 reels a call, with the
     story pages as the fallback. The profile page gives the user's id and the highlights.
  4. Every throttle is waited out through `site.back_off`, never skipped: a 429, a body asking to
     "wait a few minutes" or saying `require_login`, and a page that lands on `/accounts/login/` or
     `/challenge/`. `/accounts/suspended/` is a hard stop (`LoginError`): the account itself is in
     trouble, and waiting would only make it worse.

The API is sent through flnd's own client first, with Instagram's web headers (`prepare`). When
Instagram refuses that client, the same requests go from the logged-in tab for the rest of the run.
Requests are paced by Instaloader's measured budgets: 275 GraphQL calls per 600 s and 75 `/api/v1/`
calls per 660 s, at least 1 s apart.

How it works: `user` returns a `Profile`, which every category receives. The tabs fill it with the
profile's id, picture and highlights as they are read, so `stories`, `highlights` and `profile` load the
profile's page only when no tab did. Each category yields `Post`s; the engine names and keys them
with the formats below, which equal what the source wrote before routes, except for the date,
which is now the UTC day. A file whose URL doesn't end in its extension, and every file of a
lazy post, is named here instead (`File(name=)`) so its name stays the same.

Folder layout, under `.flnd/downloads/ins/<username>/`:
  posts/, reels/, tagged/, reposts/   <YYYY-MM-DD>_<code>_<idx>.jpg (or .mp4; no _<idx> for one file)
Videos are saved from their DASH manifest when they have one: the best video, merged with its audio.
  stories/                            <YYYY-MM-DD>_<story_id>.jpg (or .mp4)
  highlights/<title>/                 <YYYY-MM-DD>_<story_id>.jpg (or .mp4)
  profile/                            the profile picture, named as Instagram names it
One post, `ins:https://www.instagram.com/p/<code>/`, lands in `.flnd/downloads/ins/<code>/`.

History keys: `<code>_<idx>` for a tab's or a single post's files, the path for stories, highlights
and the picture.

A post found on two tabs (a reel is on the grid too) is saved once, on the first.

    saved = collect(Instagram, "ins:example_user", pages=PAGES, options=Options(categories=("stories",)))
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Generator, Iterator, Mapping
from urllib.parse import urlsplit

from flnd.source import FetchError, File, LoginError, Pace, Post, Site, Source, day, pages, target
from flnd.sources.common import dash
from flnd.sources.common.relay import embedded_results
from flnd.sources.common.replay import Replay
from flnd.sources.common.watch import watch_responses
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
    "reposts": ("fetch__XDTUserDict.user_reposts_timeline", "xdt_api__v1__feed__user_reposts_graphql_connection"),
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
# The web client's app id and its ASBD id, which Instagram's API wants on every call (gallery-dl's values).
APP_ID = "936619743392459"
ASBD_ID = "129477"
# Where Instagram sends a session it stopped: a suspended or locked account (a hard stop), and the
# login wall or challenge it puts up while it throttles (waited out).
SUSPENDED = "/accounts/suspended"
WALLS = ("/accounts/login", "/challenge")
# The cursor variable of every profile tab's GraphQL pagination query.
CURSOR = "after"
# Reels asked for in one `reels_media` call: more is refused (gallery-dl's chunk size).
REELS_PER_CALL = 5
# A grid image within this share of the post's `original_width` is its full size, not a thumbnail.
FULL_SIZE = 0.9
# Instagram's ids carry their creation time: the milliseconds since this moment (2011-08-24 21:07:01.721
# UTC), above the lowest 23 bits (the shard and a sequence number).
EPOCH_MS = 1314220021721
ID_SHIFT = 23
# A post's code is its `pk` in base 64, in this alphabet; a longer code (a private post's) isn't.
CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
LONGEST_CODE = 11
# The statuses Instagram refuses flnd's own client with, when it wants the browser's: then the tab sends it.
REFUSED_DIRECT = frozenset({400, 401, 403})


def is_graphql(url: str) -> bool:
    return "graphql" in url


class Instagram(Source):
    """Instagram profiles and single posts: the API first, the browser's pages as the fallback."""

    code = "ins"
    name = "Instagram"
    domains = ("instagram.com",)
    needs = ("sessionid",)
    # Instaloader's measured budgets (plan/phase-6/pace.md); the floor keeps bursts at a person's speed.
    pace = Pace(1.0, budgets={"/graphql": (275, 600), "/api/v1/": (75, 660)})
    # A post's category, and a highlight's title: "posts", "highlights/<title>", or "" for a single post.
    folder = "{category}/{album}"
    # Every story link, a user's or a highlight's: a story expires, but the profile lists them all.
    unsupported = {"instagram.com/stories": "story links aren't supported; give the profile, and its stories come with it"}

    def __init__(self) -> None:
        # How this run reaches the API; one source instance serves the whole run.
        self.api = Api()

    def prepare(self, request: httpx.Request, cookies: Mapping[str, str], /) -> httpx.Request:
        """Instagram's web API headers on each request flnd's client sends to instagram.com; see `Api.headers`.

        Args:
            request: the request about to be sent.
            cookies: the login's cookies as they are now; `csrftoken` rotates, so it is read each time.
        """
        request.headers.update(self.api.headers(cookies))
        return request

    # `post` comes before `user`, so instagram.com/p/<code> is never the user "p". A post's code is
    # base64url, so it takes `-` as well as `\w`.
    @target(r"instagram.com/p/{code:[\w-]+}", r"instagram.com/reel/{code:[\w-]+}", r"instagram.com/reels/{code:[\w-]+}",
            r"instagram.com/tv/{code:[\w-]+}", r"instagram.com/*/p/{code:[\w-]+}", r"instagram.com/*/reel/{code:[\w-]+}",
            r"instagram.com/*/reels/{code:[\w-]+}", r"instagram.com/*/tv/{code:[\w-]+}")
    def post(self, site: Site, code: str) -> Iterator[Post]:
        """One post or reel, by its code: from the media API by the `pk` the code spells, else its page."""
        media = self.api.post(site, code)
        if media is not None:
            yield post_of(media)

    @target("{user}", "instagram.com/{user}")
    def user(self, site: Site, user: str) -> Profile:
        """A profile, by its user name; a tab's URL (/reels/, /tagged/) is its profile too.

        Nothing is loaded yet: a tab, or the first category that needs the profile's page, loads it.
        """
        return Profile(user)

    @user.category(newest_first=True)
    def posts(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile's posts, newest first, to the end of the grid."""
        yield from profile.read_tab(site, self.api, "posts")

    @user.category(newest_first=True)
    def reels(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile's reels, newest first; the grid lists only thumbnails, so each reel's files come from the API."""
        yield from profile.read_tab(site, self.api, "reels")

    @user.category
    def tagged(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The posts the profile is tagged in, by other people: the grid follows the tags, not the dates."""
        yield from profile.read_tab(site, self.api, "tagged")

    @user.category
    def reposts(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The posts the profile reposted, in the order of the reposts."""
        yield from profile.read_tab(site, self.api, "reposts")

    @user.category(key="")
    def stories(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile's active stories, from the reels API, else their page; their key is their path, as before routes."""
        profile.describe(site)
        reels = self.api.reels(site, [profile.id], "stories") if profile.id else None
        if reels is None:
            yield from story_posts(site, f"{SITE}/stories/{profile.user}/", "stories")
            return
        yield from reel_posts(site, reels.get(str(profile.id), []), "stories")

    @user.category(key="")
    def highlights(self, site: Site, profile: Profile) -> Iterator[Post]:
        """Each highlight's stories, in the highlight's folder, 5 highlights a call; their key is their path."""
        profile.describe(site)
        found = profile.highlights
        for start in range(0, len(found), REELS_PER_CALL):
            chunk = found[start:start + REELS_PER_CALL]
            reels = self.api.reels(site, [f"highlight:{highlight_id}" for highlight_id, _ in chunk], "highlights")
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
        """The profile picture, named as Instagram names it, so a new picture is a new file; its key is its path."""
        profile.describe(site)
        if profile.picture:
            yield Post(profile.user, [File(profile.picture, name=picture_name(profile.picture))], headers=CDN_HEADERS)


class Api:
    """How one run reaches Instagram's REST API: its headers, the www-claim it keeps, and whether the tab sends it.

        media = Api().media(site, "3141592653589793238")      # a post, or None when it isn't there

    Every call waits out a throttle (`site.back_off`) and calls again, as long as it takes, and
    stops hard on `/accounts/suspended/`. The first refusal of flnd's own client (a 400, 401 or
    403, or a login wall) moves every later call into the logged-in tab, with one warning.
    """

    def __init__(self) -> None:
        # The `X-IG-WWW-Claim` Instagram gave last (`x-ig-set-www-claim`), echoed as gallery-dl does; "0" before any.
        self.claim = "0"
        # Whether the tab sends the calls (`fetch(in_page=True)`), once Instagram refused flnd's client.
        self.in_page = False

    def headers(self, cookies: Mapping[str, str]) -> dict[str, str]:
        """The headers Instagram's web client sends its API, as gallery-dl sends them.

        Args:
            cookies: the login's cookies; `csrftoken` is echoed in `X-CSRFToken` (double-submit CSRF).

        Only for instagram.com: `prepare` adds them to flnd's own client's requests, never a CDN's,
        and a call sent from the tab carries them itself. A replayed GraphQL request keeps the page's
        own `X-FB-LSD` and `X-FB-Friendly-Name` instead (`Replay`).
        """
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
            params: the query, as httpx takes it: a dict, or (name, value) pairs for a repeated name.

        Raises:
            FetchError: a refusal that isn't a rate limit, from the tab's call (with `status`); or an
                answer that isn't JSON. The caller falls back to the page.
            LoginError: Instagram sent the call to `/accounts/suspended/`.
        """
        while True:
            try:
                response = site.fetch(url, params=params, in_page=self.in_page,
                                      headers=self.headers(site.cookies) if self.in_page else None)
            except FetchError as error:
                if error.status == 404:
                    return None
                if self._waited(site, error.response, refused=error.status in REFUSED_DIRECT):
                    continue
                raise
            if self._waited(site, response, refused=False):
                continue
            self.claim = response.headers.get("x-ig-set-www-claim") or self.claim
            try:
                return response.json
            except http.Error:
                raise FetchError(f"{url}: Instagram's answer isn't JSON") from None

    def media(self, site: Site, pk: str) -> dict | None:
        """One post by its `pk` from `/api/v1/media/<pk>/info/`, shaped as a post page's; None when it isn't there.

        Raises:
            FetchError, LoginError: as `get`.
        """
        data = self.get(site, f"{API}/media/{pk}/info/")
        items = data.get("items") if isinstance(data, dict) else None
        return items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None

    def post(self, site: Site, code: str) -> dict | None:
        """One post by its code: from `media` by the `pk` the code spells, else from its page; None when neither has it."""
        pk = pk_of_code(code)
        media = None
        if pk:
            try:
                media = self.media(site, pk)
            except FetchError as error:
                site.warn("post {code}: the media API refused it ({error}); reading its page", code=code, error=error)
        # A code that decodes to another post (a private post's longer code) is never taken for it.
        if media is not None and media.get("code") == code:
            return media
        return post_page(site, code)

    def reels(self, site: Site, reel_ids: list[Any], what: str) -> dict[str, list[dict]] | None:
        """The stories of up to 5 reels (a user's id, or `highlight:<id>`) from `/api/v1/feed/reels_media/`.

        Args:
            site: the target's site.
            reel_ids: the reels, at most `REELS_PER_CALL`.
            what: what the stories are called in a warning: "stories", "highlights".

        Returns:
            Each reel's stories by its id; None, with a warning, when the API refused, so the story
            pages are read instead.
        """
        try:
            data = self.get(site, f"{API}/feed/reels_media/", params=[("reel_ids", str(reel)) for reel in reel_ids])
        except FetchError as error:
            site.warn("{what}: the reels API refused ({error}); reading the story pages", what=what, error=error)
            return None
        if not isinstance(data, dict):
            site.warn("{what}: the reels API isn't there; reading the story pages", what=what)
            return None
        found: dict[str, list[dict]] = {}
        for reel in data.get("reels_media") or list((data.get("reels") or {}).values()):
            if isinstance(reel, dict):
                found[str(reel.get("id", ""))] = [item for item in reel.get("items") or [] if isinstance(item, dict)]
        return found

    def _waited(self, site: Site, response: http.Response | None, *, refused: bool) -> bool:
        """Whether a call's answer was a throttle, now waited out, or a refusal that moved the calls into the tab.

        Args:
            site: the target's site.
            response: the answer, or a refusal's; None when there was none (a refusal with no body kept).
            refused: whether it was a 400, 401 or 403, which flnd's own client may get where the tab wouldn't.

        Raises:
            LoginError: it landed on `/accounts/suspended/`.
        """
        where = urlsplit(response.url).path if response is not None else ""
        if where.startswith(SUSPENDED):
            raise suspended()
        data = _json(response)
        walled = any(where.startswith(wall) for wall in WALLS) or (isinstance(data, dict) and data.get("require_login"))
        if (refused or walled) and not self.in_page and not throttled(data):
            # 2026-11, to be checked live: Instagram may refuse httpx's TLS fingerprint; the tab's own fetch has Chrome's.
            self.in_page = True
            site.warn("Instagram refused flnd's own client; sending its API calls from the page")
            return True
        if throttled(data) or walled:
            site.back_off("Instagram asked to wait a few minutes" if throttled(data) else "Instagram showed a login wall")
            return True
        return False


@dataclass
class Profile:
    """What the profile's results told so far: its id, picture, highlights, and which posts the tabs listed.

    Args:
        user: the user name.
    """

    user: str
    # The user's id (`pk`), which the reels API takes, once a result describing the user came.
    id: str | None = None
    # The largest profile picture, once a result describing the user came.
    picture: str | None = None
    described: bool = False
    highlights: list[tuple[str, str]] = field(default_factory=list)
    # The codes each tab listed, so a post a result repeats is yielded once.
    listed: dict[str, set[str]] = field(default_factory=lambda: {tab: set() for tab in TABS})
    # The codes any tab yielded, so a reel the posts grid already lists isn't read again.
    taken: set[str] = field(default_factory=set)

    def read_tab(self, site: Site, api: Api, tab: str) -> Iterator[Post]:
        """Read one tab to its end: its embedded first page, then its own pagination request replayed.

        Args:
            site: the target's site.
            api: the run's API, for the lazy posts' files.
            tab: one of `TABS`.

        The responses are watched from before the page loads. Scrolling stops at the first response
        that carries the tab's next page, whose request is then replayed with each cursor. When the
        tab has no further page, or no request can be replayed, scrolling reads it to the end.
        """
        replay = cursor = None
        ended = False
        with watch_responses(site, is_graphql, scroll=True) as responses:
            if not load(site, f"{SITE}/{self.user}/{TABS[tab]}"):
                return
            for result in embedded_results(site.page.document):
                yield from self._posts(site, api, result, tab)
                state = page_state(result, tab)
                if state is not None:
                    ended, cursor = not state[0], state[1]
            if not ended:
                for result in responses:
                    if throttled(result):
                        # Waited out here; the scroll's 10 s without progress start again after it.
                        site.back_off("Instagram asked to wait a few minutes")
                        continue
                    yield from self._posts(site, api, result, tab)
                    state = page_state(result, tab)
                    if state is None:
                        continue
                    ended, cursor = not state[0], state[1]
                    replay = None if ended else Replay.of(responses.request, cursor=CURSOR)
                    if ended or replay is not None:
                        break
        if replay is not None and cursor:
            refused = yield from self._replayed(site, api, tab, replay, cursor)
            if refused:
                # The tab is still where the scroll left it: scrolling on reads the rest, repeats skipped.
                with watch_responses(site, is_graphql, scroll=True) as responses:
                    for result in responses:
                        if throttled(result):
                            site.back_off("Instagram asked to wait a few minutes")
                            continue
                        yield from self._posts(site, api, result, tab)
        site.info("{tab}: {posts} listed", tab=tab, posts=fmt.count(len(self.listed[tab]), "post"))

    def describe(self, site: Site) -> None:
        """Load the profile's own page for its id, picture and highlights, unless a tab's results gave them."""
        if self.described:
            return
        if load(site, f"{SITE}/{self.user}/"):
            for result in embedded_results(site.page.document):
                self.note(result)

    def note(self, result: Any) -> None:
        """Note the profile's id, picture and highlights from one result."""
        if not isinstance(result, dict):
            return
        # Only the profile's first result describes its user; later pages may describe others.
        user = selector.parse(result).jmespath("data.user").first
        picture = profile_picture(result)
        if picture and not self.described and isinstance(user, dict):
            self.picture, self.described = picture, True
            found = user.get("pk") or user.get("id")
            self.id = str(found) if found else None
        self.highlights += [found for found in listed_highlights(result) if found not in self.highlights]

    def _replayed(self, site: Site, api: Api, tab: str, replay: Replay, cursor: str) -> Generator[Post, None, bool]:
        """The tab's pages from `cursor` on, by replay; the generator's value is True when a replay was refused.

        A rate limit is waited out and the same page asked again (`replay_page`); anything else that
        goes wrong (a refusal, an answer that isn't JSON or doesn't hold the tab) is a refusal, and
        the caller scrolls instead, warning once.
        """
        try:
            for result in pages(lambda following: replay_page(site, replay, following or cursor, tab),
                                lambda result: next_cursor(result, tab)):
                yield from self._posts(site, api, result, tab)
        except FetchError as error:
            site.warn("{tab}: the page's API refused a replay ({error}); scrolling instead", tab=tab, error=error)
            return True
        return False

    def _posts(self, site: Site, api: Api, result: Any, tab: str) -> Iterator[Post]:
        """The posts of one result this tab didn't list before: whole ones, or lazy ones whose files come from the API."""
        if not isinstance(result, dict):
            return
        self.note(result)
        for media in tab_media(result, tab):
            code = post_name(media)
            if not code or code in self.listed[tab]:
                continue
            self.listed[tab].add(code)
            if code in self.taken:
                continue
            self.taken.add(code)
            if is_complete(media):
                if post_files(media):
                    yield post_of(media)
                continue
            pk = media_pk(media)
            # Dated by its pk, so the engine skips it by `--time` before any request, and by its key when saved.
            yield Post(code, lambda code=code, pk=pk: lazy_files(site, api, code, pk), date=pk_stamp(pk),
                       headers=CDN_HEADERS)


def load(site: Site, url: str) -> bool:
    """Load an Instagram page, waiting out a login wall or a challenge; False when it didn't load.

    Raises:
        LoginError: the page went to `/accounts/suspended/`.
    """
    while True:
        if not site.goto(url):
            return False
        where = urlsplit(site.page.url).path
        if where.startswith(SUSPENDED):
            raise suspended()
        if not any(where.startswith(wall) for wall in WALLS):
            return True
        # 2026-11: a throttled session is sent to the login wall or a challenge; it lifts after a wait.
        site.back_off("Instagram showed a login wall")


def suspended() -> LoginError:
    """The hard stop for an account Instagram suspended or locked."""
    return LoginError("ins: Instagram suspended or locked this account; check it in a browser")


def replay_page(site: Site, replay: Replay, cursor: str, tab: str) -> Any:
    """One page of a tab by replay, a throttle waited out and the same page asked again.

    Raises:
        FetchError: the replay was refused, its answer isn't JSON, or it doesn't hold the tab.
        LoginError: it landed on `/accounts/suspended/`.
    """
    while True:
        try:
            response = replay.send(site, cursor)
        except FetchError as error:
            if error.response is not None and urlsplit(error.response.url).path.startswith(SUSPENDED):
                raise suspended() from None
            if throttled(_json(error.response)):
                site.back_off("Instagram asked to wait a few minutes")
                continue
            raise
        if urlsplit(response.url).path.startswith(SUSPENDED):
            raise suspended()
        data = _json(response)
        if throttled(data):
            site.back_off("Instagram asked to wait a few minutes")
            continue
        if not isinstance(data, dict) or connection_of(data, tab) is None:
            raise FetchError("its answer doesn't hold the tab's posts")
        return data


def lazy_files(site: Site, api: Api, code: str, pk: str | None) -> list[File]:
    """The files of a grid post the grid lists incompletely: from the media API, else its page; none outside `--time`.

    Args:
        site: the target's site.
        api: the run's API.
        code: the post's code, which its page is found by.
        pk: its id, which the media API is asked by; None reads the page.
    """
    media = None
    if pk:
        try:
            media = api.media(site, pk)
        except FetchError as error:
            site.warn("post {code}: the media API refused it ({error}); reading its page", code=code, error=error)
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
    """Open one post's page and return the post it embeds, or None (with a warning) when it holds none."""
    if not load(site, f"{SITE}/p/{code}/"):
        return None
    found = [media for result in embedded_results(site.page.document) if (media := post_media(result))]
    if not found:
        site.warn("post {code}: its page holds no post; skipped", code=code)
        return None
    return found[0]


def post_of(media: dict) -> Post:
    """A post with its date and files read: the engine reads `taken_at` as the date."""
    return Post(post_name(media), files_of(media), date=media.get("taken_at"), headers=CDN_HEADERS)


def files_of(media: dict, *, pinned: bool = False) -> list[File]:
    """Each file of a post, named as before routes: `<date>_<code>[_<n>].<ext>`.

    Args:
        media: the post's JSON.
        pinned: name every file here, for a post whose date the engine doesn't know. Otherwise
            only a file whose URL doesn't end in its extension is: the formats would give it another.
    """
    found = post_files(media)
    code, taken = post_name(media), day(media.get("taken_at"))
    files = []
    for index, (url, extension, audio) in enumerate(found):
        stem = f"{code}_{index}" if len(found) > 1 else code
        named = pinned or PurePosixPath(urlsplit(url).path).suffix.lower() != f".{extension}"
        files.append(File(url, audio=audio, name=file_name(taken, stem, extension) if named else None))
    return files


def story_posts(site: Site, url: str, folder: str) -> Iterator[Post]:
    """Open a story or highlight page and yield one post for each of its stories: the fallback of the reels API.

    Args:
        site: the target's site.
        url: the stories' page.
        folder: what the stories are called in the log: "stories", "highlights/<title>".
    """
    if not load(site, url):
        return
    stories = [story for result in embedded_results(site.page.document) for story in story_items(result)]
    yield from reel_posts(site, stories, folder)


def reel_posts(site: Site, stories: list[dict], folder: str) -> Iterator[Post]:
    """One post for each story of a reel, from the reels API or a story page; one line says how many.

    A story is dated by its `taken_at` only, never by its `pk`, whose small numbers would give 2011.
    """
    for story in stories:
        file = best_file(story)
        if file is None:
            continue
        source, extension, audio = file
        story_id = str(story.get("pk") or story.get("id", "")).split("_")[0]
        # The key is the path, so the extension in the name has to be the one the file is saved with.
        name = file_name(day(story.get("taken_at")), story_id, extension)
        yield Post(story_id, [File(source, audio=audio, name=name)], date=story.get("taken_at"), headers=CDN_HEADERS)
    site.info("{folder}: {stories}", folder=folder, stories=fmt.count(len(stories), "story", plural="stories"))


def file_name(taken: Any, stem: str, extension: str) -> str:
    """A file's name: `2026-01-02_C1abc_0.jpg`, or `C1abc_0.jpg` for a post without a date."""
    return f"{taken}_{stem}.{extension}" if taken else f"{stem}.{extension}"


def post_name(post: dict) -> str:
    """A post's code, as in its URL, or its number when the result leaves the code out."""
    return str(post.get("code") or post.get("pk") or "")


def media_pk(media: dict) -> str | None:
    """A listed post's id, which the media API takes: its `pk`, or its `id` before the `_<owner>`; None without one."""
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
    """The `pk` a post's code spells in base 64 (`CODE_ALPHABET`); None for a code that isn't one."""
    if not code or len(code) > LONGEST_CODE or any(letter not in CODE_ALPHABET for letter in code):
        return None
    number = 0
    for letter in code:
        number = number * 64 + CODE_ALPHABET.index(letter)
    return str(number)


def picture_name(url: str) -> str:
    """A profile picture's file name, as Instagram's URL names it, so a new picture is a new file."""
    return urlsplit(url).path.rsplit("/", 1)[-1] or "profile.jpg"


def connection_of(result: Any, tab: str) -> dict | None:
    """The connection one profile tab's result lists its posts in, or None when the result isn't the tab's."""
    found = selector.parse(result)
    for path in TAB_CONNECTIONS[tab]:
        connection = found.jmespath(f"data.{path}").first
        if isinstance(connection, dict):
            return connection
    return None


def tab_media(result: Any, tab: str) -> list[dict]:
    """The posts one profile tab's result lists: `tab_media(result, "reels")`."""
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
    """Whether a tab's result says more pages follow, and the cursor to ask for them; None when it doesn't say.

    A result that isn't the tab's, or whose connection has no `page_info`, says nothing: the
    scroll then goes on, as before replays.
    """
    connection = connection_of(result, tab) if isinstance(result, dict) else None
    info = connection.get("page_info") if connection is not None else None
    if not isinstance(info, dict):
        return None
    cursor = info.get("end_cursor")
    return bool(info.get("has_next_page") and cursor), str(cursor) if cursor else None


def next_cursor(result: Any, tab: str) -> str | None:
    """The cursor of the page after this one; None when it is the last."""
    state = page_state(result, tab)
    return state[1] if state is not None and state[0] else None


def is_complete(media: dict) -> bool:
    """Whether a grid item is the whole post, to be saved as listed (gallery-dl's rule, plan/phase-6/ins.md §3).

    It must be a plain feed post (`product_type` "feed": not a reel, a carousel or an IGTV video),
    have its date, and each of its items must be whole: a video with its video files, an image
    within 10% of its `original_width`. Anything else is read through the media API.
    """
    if media.get("product_type") != "feed" or not media.get("taken_at"):
        return False
    return all(full_size(child) for child in media.get("carousel_media") or [media])


def full_size(item: dict) -> bool:
    """Whether a listed item has its full-size file: a video's files, or an image at least 90% of its original width."""
    if item.get("media_type") == 2 or item.get("video_versions"):
        return bool(item.get("video_versions") or item.get("video_dash_manifest"))
    images = selector.parse(item).jmespath("image_versions2.candidates").all
    original = item.get("original_width")
    if not images or not isinstance(original, (int, float)) or original <= 0:
        return False
    return (max(images, key=_area).get("width") or 0) >= FULL_SIZE * original


def post_media(result: Any) -> dict | None:
    """The post a post's own page shows, or None when this result isn't it."""
    media = selector.parse(result).jmespath(POST).first
    return media if isinstance(media, dict) else None


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
    """One item's best file as `(url, extension, audio)`: its DASH video, its largest progressive one, or its largest image.

    `audio` is the DASH audio stream's URL, merged into the video; None for anything else.
    """
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
