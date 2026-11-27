"""Instagram on plugin API 2: a profile's tabs, stories, highlights and picture, one post by its
link, and the accounts the login follows, read from the pages. SPEC.md#ins-instagram.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, NamedTuple
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from flnd.plugin import (
    Blocked,
    Browser,
    Changed,
    Ctx,
    File,
    LoginError,
    NetworkError,
    NotFound,
    Plugin,
    Post,
    Request,
    Response,
    SiteError,
    Stream,
    Target,
    UsageError,
    best,
    cookies,
    dig,
    headline,
    meta,
    page_info,
    route,
    wall,
)

SITE = "https://www.instagram.com"
# The web client's REST API, called from inside the logged-in tab.
API = f"{SITE}/api/v1"
# The profile's tabs, in the order they are read, and their paths under the profile.
TABS = {"posts": "", "reels": "reels/", "tagged": "tagged/", "reposts": "reposts/"}
# The CDN refuses a file request that doesn't come from the site.
CDN_HEADERS = {"Referer": f"{SITE}/"}
# Where each profile tab's result lists its posts; the name Instagram uses now comes first.
TAB_CONNECTIONS = {
    "posts": ("xdt_api__v1__feed__user_timeline_graphql_connection",
              "xig_user_by_username.polaris_ordered_timeline_connection"),
    "reels": ("fetch__XDTUserDict.clips_connection", "xdt_api__v1__clips__user__connection_v2"),
    "tagged": ("xdt_api__v1__usertags__user_id__feed_connection",),
    "reposts": ("fetch__XDTUserDict.user_reposts_timeline",
                "xdt_api__v1__feed__user_reposts_graphql_connection"),
}
# Each tab's `(more, cursor)` reader, for its feed and for its embedded first page.
TAB_PAGES = {tab: page_info(" || ".join(f"data.{name}.page_info" for name in names))
             for tab, names in TAB_CONNECTIONS.items()}
IMAGE_EXTENSIONS = {"jpg", "webp", "png", "heic"}
# Where a grid item lists whom it is pinned for: on the posts grid, and on the reels tab, as
# the web client's media JSON names them.
PINNED = ("timeline_pinned_user_ids", "clips_tab_pinned_user_ids")
# One post's page: a reel's, or any other post's (including 2026-11 SSR `xig_polaris_media`).
POST = ("data.xig_fetch_media"
        " || data.xdt_api__v1__media__shortcode__web_info.items[0]"
        " || data.xig_polaris_media.if_not_gated_logged_out"
        " || data.xig_polaris_media")
# A story page lists one user's active stories; a highlight's page lists the highlight's.
STORIES = ("data.xdt_api__v1__feed__reels_media__connection.edges[].node.items[]"
           " || data.xdt_api__v1__feed__reels_media.reels_media[].items[]")
# What Instagram's JSON says when it is rate limiting, seen 2026-11:
# {"message": "Please wait a few minutes before you try again.", "status": "fail"}.
THROTTLED = "wait a few minutes"
THROTTLE_REASON = "Instagram asked to wait a few minutes"
# The web client's app id and ASBD id, which its API calls carry (seen 2026-11).
APP_ID = "936619743392459"
ASBD_ID = "129477"
# The paths whose requests are API calls, which carry the web client's headers.
API_PATHS = ("/api/", "/graphql")
# A grid image within this share of the post's `original_width` is its full size, not a thumbnail.
FULL_SIZE = 0.9
# Instagram ids carry creation time: milliseconds since this moment (2011-08-24 21:07:01.721
# UTC), above the lowest 23 bits (the shard and a sequence number).
EPOCH_MS = 1314220021721
ID_SHIFT = 23
# Base64url alphabet Instagram encodes numeric media pks with in post shortcodes.
SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
# What a GraphQL call's URL holds: the responses that carry a profile tab's or a post's data.
GRAPHQL = "graphql"
# Captured GraphQL request headers safe to send back in-page; `sign` sets the rest.
QUERY_HEADERS = ("content-type", "x-fb-", "x-bloks-", "x-root-field-name")
# Where a story's music is, in the two shapes Instagram sends it (as gallery-dl reads it): a music
# sticker's asset, or the story's own music metadata.
STORY_MUSIC = ("story_music_stickers[0].music_asset_info.progressive_download_url || "
               "music_metadata.music_info.music_asset_info.progressive_download_url")
USER_NAME = re.compile(r"[A-Za-z0-9._]{1,30}")
LOGIN_HINT = "run flnd auth login ins"
LOCK_WALLS = (
    "/challenge", "/accounts/suspended", "/accounts/disabled",
    "/consent", "/accounts/two_factor", "/auth_platform",
)
_FATAL_META = ("checkpoint_required", "challenge_required", "login_required", "consent_required")
HIGHLIGHT_STOP = 2
MAX_RELOADS = 6


def _check_meta_error(data: Any) -> None:
    """Abort immediately on Meta checkpoint, login, consent, or action-block JSON payloads."""
    if not isinstance(data, dict):
        return
    msg = str(data.get("message", "")).lower()
    err = str(data.get("error_type", "")).lower()
    if (data.get("require_login") is True or data.get("checkpoint_url")
            or data.get("challenge") or any(k in msg or k in err for k in _FATAL_META)):
        raise LoginError(
            "ins: account requires action or login; resolve in browser",
            hint=LOGIN_HINT, retryable=False,
        )
    if (data.get("spam") is True or data.get("feedback_required") is True
            or "feedback_required" in msg or "feedback_required" in err):
        raise Blocked("ins: action blocked by Instagram (feedback_required)", retryable=False)


async def _api_fetch(ctx: Ctx, request: str | Request) -> Response:
    """Fetch an API endpoint: direct HTTP first, falling back to in_page=True on error."""
    try:
        return await ctx.fetch(request, in_page=False)
    except (SiteError, NetworkError) as error:
        if isinstance(error, Blocked):
            raise
        return await ctx.fetch(request, in_page=True)


class Instagram(Plugin):
    """Instagram profiles, single posts and the login's follows, all read from the pages."""

    code = "ins"
    name = "Instagram"
    domains = ("instagram.com",)
    # Images, media and fonts load until a live check shows its pages work without them.
    hidden = True
    # Logged in when `sessionid` is there. Not required: a public profile shows something.
    login = cookies("sessionid")
    browser = Browser.ANY
    # Instagram rate-limits bursts of `/p/<code>/` and GraphQL page requests; 2 s keeps a long
    # sync under the limit.
    pace = 2.0
    # A throttled session is sent to the login wall, which lifts after a wait; lock/challenge
    # walls abort immediately in `blocked()`.
    walls = ("/accounts/login",)
    fields = ("album", "title")
    # The day, the caption's first words, the code, and the file's number when the post has more
    # than one. Stories, highlights and the picture name their own files.
    filename = "{date}_{title:.40}_{post}{index}"
    # Every story link, a user's or a highlight's: a story expires, but the profile lists them all.
    unsupported = {
        "instagram.com/stories": (
            "story links aren't supported; give the profile, and its stories come with it: "
            'the user with |stories or |highlights, e.g. "ins:example_user|highlights"'
        ),
        "instagram.com/explore": "that's not a profile",
        "instagram.com/accounts": "that's not a profile",
        "instagram.com/direct": "that's not a profile",
    }

    @classmethod
    def resolve(cls, text: str) -> Target | None:
        """The routes' reading, then the name rule: a user name is `USER_NAME`.

        Raises:
            UsageError: a name that isn't one, or one of the site's own pages.
        """
        found = super().resolve(text)
        if found is not None and found.kind == "user" and not USER_NAME.fullmatch(found.value):
            raise UsageError(f"ins: {found.value!r} isn't a user name; "
                             "a user name is 1 to 30 letters, digits, . or _")
        return found

    def __init__(self) -> None:
        # The `X-IG-WWW-Claim` Instagram gave last, echoed on the next API calls (2026-11-12).
        self.claim = "0"

    def sign(self, request: Request, cookies: Mapping[str, str]) -> Request:
        """Add the web client's headers to an API call; a page's request is left as it is."""
        if not urlsplit(request.url).path.startswith(API_PATHS):
            return request
        sent = {"X-IG-App-ID": APP_ID, "X-ASBD-ID": ASBD_ID, "X-IG-WWW-Claim": self.claim,
                "X-Requested-With": "XMLHttpRequest", "Referer": f"{SITE}/"}
        if cookies.get("csrftoken"):
            sent["X-CSRFToken"] = cookies["csrftoken"]
        return request.replace(headers={**sent, **(request.headers or {})})

    def blocked(self, response: Response) -> float | None:
        """Abort on lock walls or Meta error payloads; hold on "please wait"."""
        found = wall(getattr(response, "url", ""), LOCK_WALLS)
        if found is not None:
            raise LoginError(
                f"ins: account requires action ({found}); resolve in browser",
                hint=LOGIN_HINT, retryable=False,
            )
        parsed = _json(response)
        _check_meta_error(parsed)
        return 0.0 if throttled(parsed) else None

    # `post` comes before `user`, so instagram.com/p/<code> is never the user "p". A post's code is
    # base64url, so it takes `-` as well as `\w`.
    @route(*((pattern, "post/{code}") for pattern in (
        r"ins:post/{code:[\w-]+}", r"instagram.com/p/{code:[\w-]+}",
        r"instagram.com/reel/{code:[\w-]+}", r"instagram.com/reels/{code:[\w-]+}",
        r"instagram.com/tv/{code:[\w-]+}", r"instagram.com/*/p/{code:[\w-]+}",
        r"instagram.com/*/reel/{code:[\w-]+}", r"instagram.com/*/reels/{code:[\w-]+}",
        r"instagram.com/*/tv/{code:[\w-]+}",
    )), single=True)
    async def post(self, ctx: Ctx, code: str) -> AsyncIterator[Post]:
        """One post or reel: `ins:post/C1abcDEFghi`, or its page, `instagram.com/p/C1abcDEFghi`.

        Raises:
            NotFound: the page holds no post of that code, or one without files.
        """
        code = code.removeprefix("post/")
        if account_id(ctx.cookies) is not None:
            media = await logged_in_post(ctx, code, self)
        else:
            media = await opened_post(ctx, code)
        if not post_files(media):
            raise NotFound(f"ins: post {code} has no files")
        yield post_of(media)

    @route("ins:{user}", "instagram.com/{user}")
    async def user(self, ctx: Ctx, user: str) -> Profile:
        """A profile: `ins:example_user`, or `https://www.instagram.com/example_user/`.

        Nothing is loaded yet: a tab, or the first category that needs the profile's page,
        loads it.
        """
        return Profile(user, plugin=self)

    @user.category
    async def posts(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """The posts grid, newest first (up to 3 pinned ones above), to its end."""
        async for post in read_tab(ctx, profile, "posts"):
            yield post

    @user.category(url=f"{SITE}/{{user}}/reels/")
    async def reels(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """The reels tab, newest first."""
        async for post in read_tab(ctx, profile, "reels"):
            yield post

    @user.category(own=False, url=f"{SITE}/{{user}}/tagged/", newest_first=False)
    async def tagged(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """The posts the profile is tagged in, in the tab's order."""
        async for post in read_tab(ctx, profile, "tagged"):
            yield post

    @user.category(own=False, url=f"{SITE}/{{user}}/reposts/", newest_first=False)
    async def reposts(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """The posts the profile reposted, latest repost first (each post has its own date)."""
        async for post in read_tab(ctx, profile, "reposts"):
            yield post

    @user.category(url=f"{SITE}/stories/{{user}}/", newest_first=False, key="{path}")
    async def stories(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """The active stories, from the story page; keyed by path."""
        for post in await story_page(ctx, f"{SITE}/stories/{profile.user}/", "stories"):
            yield post

    @user.category(newest_first=False, folder="highlights/{album}", key="{path}")
    async def highlights(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """Each highlight's stories, in a folder named by its title; keyed by path."""
        await profile.describe(ctx)
        listing = getattr(ctx, "listing", None)
        sync = bool(getattr(listing, "sync", False))
        unchanged = 0
        for index, (highlight, title) in enumerate(profile.highlights):
            if index:
                await ctx.pause(random.uniform(2.0, 5.0), "pausing between highlights")
            url = f"{SITE}/stories/highlights/{highlight}/"
            before = getattr(listing, "queued", 0)
            stories = await story_page(ctx, url, f"highlights/{title}")
            for post in stories:
                yield post.replace(album=title)
            if sync and stories:
                if getattr(listing, "queued", 0) > before:
                    unchanged = 0
                else:
                    unchanged += 1
                    if unchanged >= HIGHLIGHT_STOP:
                        return

    @user.category(newest_first=False, folder="profile", key="{path}")
    async def profile(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """The profile picture, at its largest, under the CDN's own name; keyed by path."""
        await profile.describe(ctx)
        if profile.picture:
            picture = File(profile.picture, name=picture_name(profile.picture))
            yield Post(profile.user, [picture], headers=CDN_HEADERS, title="")


def throttled(result: Any) -> bool:
    """Whether a JSON answer is Instagram asking to slow down instead of a result."""
    return isinstance(result, dict) and THROTTLED in str(result.get("message", ""))


def _json(response: Response) -> Any:
    """A response's JSON object; None for any other body, which isn't parsed."""
    # Only a body that starts as an object can be the throttle answer; a page is never parsed.
    if getattr(response, "content", b"")[:1] != b"{":
        return None
    try:
        return response.json
    except Changed:
        return None


def account_id(cookies: Mapping[str, str]) -> str | None:
    """The login's numeric id: its `ds_user_id` cookie, else its `sessionid`'s prefix."""
    found = cookies.get("ds_user_id", "")
    if found.isdigit():
        return found
    # A `sessionid` starts with its account's id: `<id>%3A<rest>`.
    prefix = unquote(cookies.get("sessionid", "")).split(":", 1)[0]
    return prefix if prefix.isdigit() else None


# ---------------------------------------------------------------------------------------------
# The profile and its tabs
# ---------------------------------------------------------------------------------------------

class Profile(Mapping[str, Any]):
    """What the profile's pages said so far: its picture, highlights, and each tab's posts.

    As a mapping it holds `user`, which the categories' `url` templates read.

    Args:
        user: the user name, as the target gives it.
    """

    def __init__(self, user: str, *, plugin: Instagram | None = None) -> None:
        self.user = user
        self.plugin = plugin
        self.id: str | None = None
        # The largest profile picture, once a result describing the user came.
        self.picture: str | None = None
        # Whether a result described the user, so the profile page isn't loaded for it.
        self.described = False
        # Whether the profile page loaded: read once, even when it brought no user result.
        self.loaded = False
        # The highlights as `(id, title)`, in the order the results list them.
        self.highlights: list[tuple[str, str]] = []
        # The codes each tab listed, so a post a result repeats is yielded once.
        self.listed: dict[str, set[str]] = {tab: set() for tab in TABS}

    def __getitem__(self, key: str) -> Any:
        if key != "user":
            raise KeyError(key)
        return self.user

    def __iter__(self) -> Iterator[str]:
        return iter(("user",))

    def __len__(self) -> int:
        return 1

    async def describe(self, ctx: Ctx) -> None:
        """Load the profile page once for its picture and highlights, unless a tab told them.

        2026-11: the page no longer embeds the user or the highlights; its own GraphQL calls
        bring them, so those are read too.
        """
        if self.described or self.loaded:
            return
        for result in await page_results(ctx, f"{SITE}/{self.user}/"):
            self.note(result)
        self.loaded = True

    def note(self, result: Any) -> None:
        """Note the profile's id, picture and highlights from one result of its pages."""
        if not isinstance(result, dict):
            return
        user = dig(result, "data.user || data.xig_user_by_username")
        if isinstance(user, dict):
            user_id = user.get("pk") or user.get("id")
            if user_id and str(user_id).isdigit() and self.id is None:
                self.id = str(user_id)
            picture = (dig(user, "hd_profile_pic_url_info.url")
                       or user.get("profile_pic_url_hd")
                       or user.get("profile_pic_url"))
            if isinstance(picture, str) and (not self.described or self.picture is None):
                self.picture = picture
            if not self.described and (picture or "hd_profile_pic_url_info" in user):
                self.described = True
        self.highlights += [h for h in listed_highlights(result) if h not in self.highlights]


async def page_results(
    ctx: Ctx, url: str, *, enough: Callable[[list[Any]], bool] | None = None,
) -> list[Any]:
    """Every result of a page opened without a scroll: the ones it embeds, then its calls'.

    A "please wait" answer among them holds, and the page is opened again, so a throttle is
    never read as a page without a post or a profile without highlights.

    Args:
        enough: whether the results hold what the caller wants, a throttle beside them or
            not; None wants every answer unthrottled.
    """
    while True:
        found: list[Any] = []
        async with aclosing(ctx.load(url, GRAPHQL, scroll=False)) as listing:
            embedded = True
            async for item in listing:
                if embedded:
                    found.extend(item)
                    embedded = False
                else:
                    found.append(item)
        for item in found:
            _check_meta_error(item)
        if (enough is not None and enough(found)) or not any(map(throttled, found)):
            return found
        await ctx.hold(THROTTLE_REASON, seconds=random.uniform(300.0, 600.0))


class TabQuery(NamedTuple):
    """A captured GraphQL tab query (`POST /api/graphql`), paged in-page via `variables.after`."""

    url: str
    headers: dict[str, str]
    pairs: tuple[tuple[str, str], ...]
    var_index: int
    variables: dict[str, Any]


def parse_tab_query(request: Request | None) -> TabQuery | None:
    """Extract a replayable paginated `TabQuery` from a captured `/graphql` POST; None else."""
    if (request is None or request.method != "POST" or not isinstance(request.body, str)
            or GRAPHQL not in urlsplit(request.url).path):
        return None
    pairs = parse_qsl(request.body, keep_blank_values=True)
    for idx, (key, raw) in enumerate(pairs):
        if key != "variables":
            continue
        try:
            variables = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(variables, dict) or "after" not in variables:
            return None
        headers = {k: v for k, v in (request.headers or {}).items()
                   if k.lower().startswith(QUERY_HEADERS)}
        if not any(k.lower() == "content-type" for k in headers):
            headers["content-type"] = "application/x-www-form-urlencoded"
        return TabQuery(request.url, headers, tuple(pairs), idx, variables)
    return None


async def _graphql_fetch(ctx: Ctx, query: TabQuery, cursor: str) -> Response:
    """Send one read-only GraphQL POST in-page, retrying transient 5xx or network blips."""
    pairs = list(query.pairs)
    pairs[query.var_index] = ("variables", json.dumps({**query.variables, "after": cursor},
                                                      separators=(",", ":")))
    req = Request(query.url, method="POST", headers=query.headers, body=urlencode(pairs))
    for attempt in range(3):
        try:
            return await ctx.fetch(req, in_page=True)
        except (NetworkError, SiteError) as error:
            transient = isinstance(error, NetworkError) or (error.status or 0) >= 500
            if attempt == 2 or not transient or isinstance(error, (Blocked, LoginError)):
                raise
            await ctx.pause(random.uniform(3.0, 7.0) * (attempt + 1), "retrying GraphQL page")
    raise AssertionError("unreachable")


async def _tab_pause(ctx: Ctx, page_num: int) -> None:
    """Humanized pause between tab pages, with a longer break every 15 pages."""
    delay = random.uniform(12.0, 25.0) if page_num % 15 == 0 else random.uniform(2.0, 5.0)
    await ctx.pause(delay, "pausing between tab pages")


async def _graphql_pages(
    ctx: Ctx, profile: Profile, tab: str, query: TabQuery, cursor: str,
    reader: Callable[[Any], tuple[bool, str | None] | None], counts: list[int],
) -> AsyncIterator[Post]:
    """Continue a profile tab past its first scrolled page via in-page GraphQL POSTs."""
    seen = {cursor}
    while cursor:
        counts[0] += 1
        counts[1] += 1
        await _tab_pause(ctx, counts[0])
        response = await _graphql_fetch(ctx, query, cursor)
        if profile.plugin is not None:
            claim = response.headers.get("x-ig-set-www-claim")
            profile.plugin.claim = claim or profile.plugin.claim
        result = response.json
        _check_meta_error(result)
        profile.note(result)
        for post in new_posts(ctx, profile, result, tab):
            yield post
        state = reader(result)
        if state is None:
            raise Changed(f"ins: {tab} GraphQL page has no connection")
        more, following = state
        if not more or not following or following in seen:
            return
        seen.add(following)
        cursor = following


async def read_tab(ctx: Ctx, profile: Profile, tab: str) -> AsyncIterator[Post]:
    """The posts of one tab in its order, until its `page_info` says it ended.

    Scrolls until the tab sends a paginated GraphQL POST (`variables.after`), then continues via
    in-page GraphQL POSTs so long tabs neither bloat the DOM nor stall on scroll.

    Raises:
        NotFound, LoginError: the tab's page didn't load.
    """
    reader = TAB_PAGES[tab]
    url = f"{SITE}/{profile.user}/{TABS[tab]}"
    counts = [0, 0]
    query: TabQuery | None = None
    next_cursor: str | None = None
    reloads = 0
    try:
        while True:
            got_new = False
            listing = ctx.load(url, GRAPHQL, cursor=reader)
            try:
                async with aclosing(listing):
                    embedded = True
                    async for item in listing:
                        ended = False
                        tab_page = False
                        for result in item if embedded else [item]:
                            _check_meta_error(result)
                            if throttled(result):
                                await ctx.hold(THROTTLE_REASON,
                                               seconds=random.uniform(300.0, 600.0))
                                continue
                            profile.note(result)
                            for post in new_posts(ctx, profile, result, tab):
                                got_new = True
                                yield post
                            state = reader(result)
                            if state is not None:
                                tab_page = True
                                if not (state[0] and state[1]):
                                    ended = True
                                else:
                                    next_cursor = state[1]
                                    if not embedded and query is None:
                                        query = parse_tab_query(ctx.request)
                        was_embedded, embedded = embedded, False
                        if ended:
                            return
                        if not was_embedded and tab_page:
                            counts[0] += 1
                        if query is not None and next_cursor is not None:
                            break
                        if not was_embedded and tab_page:
                            await _tab_pause(ctx, counts[0])
                break
            except Blocked as error:
                if not error.retryable:
                    raise
                if (reloads > 0 and not got_new) or reloads >= MAX_RELOADS:
                    if getattr(ctx, "listing", None) is not None:
                        ctx.listing.stalled = True
                    raise Blocked(f"{error} (no progress after reload)",
                                  wait=error.wait, retryable=False) from None
                await ctx.hold("rate limited", seconds=error.wait, reset=got_new)
                reloads += 1
        if query is not None and next_cursor is not None:
            async for post in _graphql_pages(ctx, profile, tab, query, next_cursor, reader, counts):
                yield post
    finally:
        ctx.log("debug", "{user}: {tab}: read {scrolled} scrolled, {gql} graphql pages",
                user=profile.user, tab=tab, scrolled=counts[0] - counts[1], gql=counts[1])


def new_posts(ctx: Ctx, profile: Profile, result: Any, tab: str) -> Iterator[Post]:
    """The posts one result of a tab lists that the tab hasn't listed yet."""
    for media in tab_media(result, tab):
        code = post_name(media)
        if not code or code in profile.listed[tab]:
            continue
        profile.listed[tab].add(code)
        # A pinned post sits above newer ones (2026-11-14): the sync's stops skip it.
        pinned = is_pinned(media)
        if not is_complete(media):
            if _placeholder_post(media):
                continue
            yield lazy_post(ctx, code, media_pk(media), profile.plugin).replace(pinned=pinned)
        elif post_files(media):
            yield post_of(media).replace(pinned=pinned)


def lazy_post(ctx: Ctx, code: str, pk: str | None, plugin: Instagram | None = None) -> Post:
    """A grid post dated by its pk, whose files are read from its page if the engine wants it.

    The pk's date lets the engine skip it by `--when` before any request; its files are named
    from the post's own `taken_at`. Without a pk it is listed undated.
    """

    async def files() -> list[File]:
        media = await post_page(ctx, code, plugin, pk=pk)
        if media.get("taken_at") not in ctx.when:
            return []
        return files_of(media, named=True)

    return Post(code, files, date=pk_stamp(pk), headers=CDN_HEADERS, title="")


# ---------------------------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------------------------

async def post_page(ctx: Ctx, code: str, plugin: Instagram | None = None, *,
                    pk: str | None = None) -> dict:
    """A post: logged in from `/api/v1/media/<pk>/info/`, otherwise from its own page.

    Raises:
        NotFound: the source holds no post of that code.
    """
    if plugin is not None and account_id(ctx.cookies) is not None:
        return await logged_in_post(ctx, code, plugin, pk)
    return await opened_post(ctx, code)


async def logged_in_post(ctx: Ctx, code: str, plugin: Instagram,
                         pk: str | None = None) -> dict:
    """Resolve one post while logged in: the media info API, then its page if unreadable."""
    if (resolved := pk or code_pk(code)) is not None:
        media = await media_info(ctx, code, resolved, plugin)
        if media is not None:
            return media
    return await opened_post(ctx, code)


async def media_info(ctx: Ctx, code: str, pk: str, plugin: Instagram) -> dict | None:
    """Fetch one post via `/api/v1/media/{pk}/info/`; None when its shape is unreadable.

    A block or a "please wait" raises `Blocked`, which the engine holds and retries.
    """
    try:
        response = await _api_fetch(ctx, f"{API}/media/{pk}/info/")
        plugin.claim = response.headers.get("x-ig-set-www-claim") or plugin.claim
        data = response.json
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise Changed("ins: media info has no items")
        if not items:
            raise NotFound(f"ins: post {code}: its page holds no post")
        item = items[0]
        if not isinstance(item, dict):
            raise Changed("ins: media info item is not an object")
        if item.get("code") and item["code"] != code and media_pk(item) != code:
            raise NotFound(f"ins: post {code}: its page holds no post")
        return item
    except Changed:
        return None


async def opened_post(ctx: Ctx, code: str) -> dict:
    """A post, from its page opened with its GraphQL calls listened to, never scrolled.

    Raises:
        NotFound: the page holds no post of that code.
    """
    def post(found: list[Any]) -> dict | None:
        return next((media for result in found
                     if (media := post_media(result, code)) is not None), None)

    media = post(await page_results(ctx, f"{SITE}/p/{code}/",
                                    enough=lambda found: post(found) is not None))
    if media is None:
        raise NotFound(f"ins: post {code}: its page holds no post")
    return media


async def story_page(ctx: Ctx, url: str, where: str) -> list[Post]:
    """A post for each story a story or highlight page lists.

    Args:
        url: the story or highlight page.
        where: the folder the stories go to, for the line counting them.

    Raises:
        SiteError: the page didn't load.
    """
    while True:
        landed = await ctx.open(url)
        if landed.status >= 400:
            # Raised, not skipped: a page that ended here would be recorded complete.
            raise SiteError(f"{where}: the page didn't load (HTTP {landed.status})",
                            status=landed.status)
        found = list(meta(await ctx.document()))
        for result in found:
            _check_meta_error(result)
        stories = [story for result in found for story in story_items(result)]
        if stories or not any(map(throttled, found)):
            break
        await ctx.hold(THROTTLE_REASON, seconds=random.uniform(300.0, 600.0))
    ctx.log("info", "{where}: {n} {unit}", where=where, n=len(stories),
            unit="story" if len(stories) == 1 else "stories")
    return [post for story in stories if (post := story_post(story)) is not None]


# ---------------------------------------------------------------------------------------------
# Posts and their files
# ---------------------------------------------------------------------------------------------

def post_of(media: dict) -> Post:
    """A post with its files read (`taken_at` is its date); a clip is a reel, else a post."""
    clip = media.get("product_type") == "clips" or "clips_metadata" in media
    return Post(post_name(media), files_of(media), date=media.get("taken_at"),
                headers=CDN_HEADERS, owner=dig(media, "user.username"),
                category="reels" if clip else "posts", title=title_of(media))


def files_of(media: dict, *, named: bool = False) -> list[File]:
    """Each file of a post, named here only where the templates would name it otherwise.

    Args:
        named: name every file here from the post's `taken_at`, as
            `<date>_<code>[_<n>].<extension>`: a lazy post is dated by its pk, which may be
            another day.
    """
    found = post_files(media)
    code, taken = post_name(media), utc_day(media.get("taken_at"))
    title = title_of(media)
    files = []
    for n, (urls, extension, audio) in enumerate(found):
        stem = f"{code}_{n}" if len(found) > 1 else code
        # A URL with another extension, or none, is named so saved names stay as they were.
        own = named or PurePosixPath(urlsplit(urls[0]).path).suffix.lower() != f".{extension}"
        name = file_name(taken, stem, extension, title) if own else None
        files.append(as_file(urls, audio, name=name))
    return files


def story_post(story: dict) -> Post | None:
    """One story as a post, its music beside it; None for a story without a file."""
    found = best_file(story)
    if found is None:
        return None
    urls, extension, audio = found
    # An `id` carries its owner after an `_` (2026-11-11); the story's own id is before it.
    story_id = str(story.get("pk") or story.get("id", "")).split("_")[0]
    taken = utc_day(story.get("taken_at"))
    # The key is the path, so a story is named as saved stories have always been.
    files = [as_file(urls, audio, name=file_name(taken, story_id, extension))]
    music = dig(story, STORY_MUSIC)
    if isinstance(music, str):
        files.append(File(music, name=file_name(taken, f"{story_id}_music", "m4a")))
    return Post(story_id, files, date=story.get("taken_at"), headers=CDN_HEADERS, title="")


def file_name(taken: str | None, stem: str, extension: str, title: str = "") -> str:
    """A file's name: `2026-01-02_Sunset_C1abc_0.jpg`; the day and the title drop when empty."""
    return "_".join(part for part in (taken, title[:40], stem) if part) + f".{extension}"


def title_of(media: dict) -> str:
    """A post's title: the first words of its caption; "" for none."""
    return headline(dig(media, "caption.text"))


def utc_day(value: Any) -> str | None:
    """The UTC day of Instagram's `taken_at` (Unix seconds) as `YYYY-MM-DD`; None without one.

    Only for the names the plugin sets, which must match the day the engine puts in `{date}`.
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
    """A post's numeric id: its `pk`, or its `id` before `_<owner>`; None for neither."""
    found = str(media.get("pk") or str(media.get("id") or "").split("_")[0])
    return found if found.isdigit() else None


def code_pk(code: str) -> str | None:
    """Decode an Instagram base64url shortcode into its 64-bit media pk; None if invalid."""
    if not code or len(code) > 11:
        return None
    pk = 0
    for char in code:
        digit = SHORTCODE_ALPHABET.find(char)
        if digit < 0:
            return None
        pk = (pk << 6) | digit
    return str(pk) if 0 < pk < (1 << 64) else None


def pk_stamp(pk: str | None) -> float | None:
    """The Unix time an Instagram id was made at, read from the id itself; None without one.

    Only a grid post is dated this way, to skip it by `--when` before any request.
    """
    if not pk or not pk.isdigit():
        return None
    return ((int(pk) >> ID_SHIFT) + EPOCH_MS) / 1000


def picture_name(url: str) -> str:
    """A profile picture's file name, the CDN's own."""
    return urlsplit(url).path.rsplit("/", 1)[-1] or "profile.jpg"


def tab_media(result: Any, tab: str) -> list[dict]:
    """The posts one profile tab's result lists, in order; [] for a result that isn't the tab's."""
    connection = next((found for name in TAB_CONNECTIONS[tab]
                       if isinstance(found := dig(result, f"data.{name}"), dict)), None)
    if connection is None:
        return []
    if "repost_grid_items" in connection:
        nodes = [item.get("media", item) for item in connection["repost_grid_items"]]
    else:
        nodes = [edge.get("node") or {} for edge in connection.get("edges", [])]
    # The reels tab wraps each post as {"media": post} (2026-11-11).
    return [node["media"] if isinstance(node.get("media"), dict) else node for node in nodes]


def is_complete(media: dict) -> bool:
    """Whether a grid item is the whole post: a dated feed photo, carousel or reel at full size."""
    if not media.get("taken_at"):
        return False
    kind = media.get("product_type")
    if kind == "carousel_container":
        children = media.get("carousel_media")
        return isinstance(children, list) and _complete_carousel(children)
    if kind == "clips":
        return bool(media.get("video_versions") or media.get("video_dash_manifest"))
    if kind == "feed":
        children = media.get("carousel_media")
        if isinstance(children, list) and children:
            return _complete_carousel(children)
        return full_size(media)
    return False


def _complete_carousel(children: list[Any]) -> bool:
    """Whether a carousel has at least one full-size slide and no incomplete non-null slides."""
    return (bool(children)
            and all(isinstance(c, dict) for c in children)
            and any(full_size(c) for c in children)
            and all(full_size(c) or _placeholder_only(c) for c in children))


def full_size(item: dict) -> bool:
    """Whether a listed item has its whole file: a video's paths, or an image near full width.

    A grid item may carry only a thumbnail (2026-11-12).
    """
    if item.get("media_type") == 2 or item.get("video_versions"):
        return bool(item.get("video_versions") or item.get("video_dash_manifest"))
    image = best(_streams(dig(item, "image_versions2.candidates")))
    original = item.get("original_width")
    if image is None or not isinstance(original, (int, float)) or original <= 0:
        return False
    return image.width >= FULL_SIZE * original


def post_media(result: Any, code: str) -> dict | None:
    """The post a post's page shows, or None when this result isn't it or names another code."""
    media = dig(result, POST)
    if not isinstance(media, dict) or (media.get("code") and media.get("code") != code):
        return None
    if "if_not_gated_logged_out" in media and not post_files(media):
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


def post_files(media: dict) -> list[tuple[tuple[str, ...], str, str | None]]:
    """Each file of a post as `best_file` gives it: every carousel item, or the post itself."""
    children = media.get("carousel_media") or [media]
    return [found for child in children if (found := best_file(child)) is not None]


def best_file(media: dict) -> tuple[tuple[str, ...], str, str | None] | None:
    """An item's best file as `(urls, extension, audio)`: its DASH pair, its MP4, or its image.

    With a DASH pair and a progressive MP4 both, the MP4 is the second URL, saved alone when
    the DASH video fails; the audio goes with the first URL only.
    """
    progressive = best(_streams(media.get("video_versions")))
    dash = File.dash(media.get("video_dash_manifest"))
    if dash is not None and dash.url is not None:
        # The joined file takes the video's extension: WebM for a WebM stream, else MP4.
        webm = PurePosixPath(urlsplit(dash.url).path).suffix.lower() == ".webm"
        urls = (dash.url,)
        if progressive is not None and progressive.url != dash.url:
            urls = (dash.url, progressive.url)
        return urls, "webm" if webm else "mp4", dash.audio
    if progressive is not None:
        return (progressive.url,), "mp4", None
    image = best(_streams(dig(media, "image_versions2.candidates")))
    return ((image.url,), image_extension(image.url), None) if image is not None else None


def image_extension(url: str) -> str:
    """An image's extension from its URL: older posts are served as WebP, newer ones as JPEG."""
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower().lstrip(".")
    return suffix if suffix in IMAGE_EXTENSIONS else "jpg"


def is_pinned(media: dict) -> bool:
    """Whether a grid item is pinned to the top of its tab: a list of whom it is pinned for."""
    return any(media.get(key) for key in PINNED)


def as_file(urls: tuple[str, ...], audio: str | None, *, name: str | None = None) -> File:
    """A file from `best_file`'s URLs and audio: one URL, or mirrors tried in order."""
    if len(urls) == 1:
        return File(urls[0], audio=audio, name=name)
    return File(mirrors=urls, audio=audio, name=name)


def _placeholder(url: str) -> bool:
    """Whether `url` is Instagram's `rsrc.php/null.*` placeholder for a missing image."""
    return urlsplit(url).path.startswith("/rsrc.php/null")


def _urls_of(item: dict) -> list[str]:
    """Every image or video candidate URL on one media item."""
    found: list[str] = []
    if isinstance(versions := item.get("video_versions"), list):
        found.extend(v["url"] for v in versions if isinstance(v, dict) and v.get("url"))
    if isinstance(candidates := dig(item, "image_versions2.candidates"), list):
        found.extend(c["url"] for c in candidates if isinstance(c, dict) and c.get("url"))
    return found


def _placeholder_only(item: dict) -> bool:
    """Whether an item has candidate URLs and every one is a `rsrc.php/null.*` placeholder."""
    if item.get("video_dash_manifest"):
        return False
    urls = _urls_of(item)
    return bool(urls) and all(_placeholder(url) for url in urls)


def _placeholder_post(media: dict) -> bool:
    """Whether a full feed item carries only `rsrc.php/null.*` placeholder media."""
    if not media.get("taken_at"):
        return False
    children = media.get("carousel_media")
    if isinstance(children, list) and children:
        return all(isinstance(c, dict) and _placeholder_only(c) for c in children)
    if "image_versions2" in media:
        return _placeholder_only(media)
    return False


def _streams(versions: Any) -> list[Stream]:
    """An item's image or video versions as streams, each with its URL and size."""
    if not isinstance(versions, list):
        return []
    return [Stream(version["url"], height=version.get("height") or 0,
                   width=version.get("width") or 0)
            for version in versions
            if isinstance(version, dict) and version.get("url")
            and not _placeholder(version["url"])]

