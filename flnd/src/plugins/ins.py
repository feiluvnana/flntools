"""Instagram on plugin API 2: a profile's tabs, stories, highlights and picture, one post by its
link, and the accounts the login follows, read from the pages; the ins page in src/docs/plugins/.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from flnd.plugin import (
    Browser,
    Changed,
    Ctx,
    File,
    LoginError,
    NotFound,
    Plugin,
    Post,
    Request,
    Response,
    SiteError,
    Stream,
    Target,
    best,
    cookies,
    dig,
    meta,
    page_info,
    route,
)

SITE = "https://www.instagram.com"
# The web client's REST API; only `ins:me`'s friendships list is read from it.
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
# Each tab's `(more, cursor)` reader, for its feed and for its embedded first page.
TAB_PAGES = {tab: page_info(" || ".join(f"data.{name}.page_info" for name in names))
             for tab, names in TAB_CONNECTIONS.items()}
IMAGE_EXTENSIONS = {"jpg", "webp", "png", "heic"}
# Where a grid item lists whom it is pinned for: on the posts grid, and on the reels tab, as
# the web client's media JSON names them.
PINNED = ("timeline_pinned_user_ids", "clips_tab_pinned_user_ids")
# One post's page: a reel's, or any other post's.
POST = "data.xig_fetch_media || data.xdt_api__v1__media__shortcode__web_info.items[0]"
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
# Accounts asked for in one page of the friendships API, as the web client asks.
FOLLOWING_COUNT = 100
# A grid image within this share of the post's `original_width` is its full size, not a thumbnail.
FULL_SIZE = 0.9
# Instagram ids carry creation time: milliseconds since this moment (2011-08-24 21:07:01.721
# UTC), above the lowest 23 bits (the shard and a sequence number).
EPOCH_MS = 1314220021721
ID_SHIFT = 23
# What a GraphQL call's URL holds: the responses that carry a profile tab's or a post's data.
GRAPHQL = "graphql"
# Where a story's music is, in the two shapes Instagram sends it (as gallery-dl reads it): a music
# sticker's asset, or the story's own music metadata.
STORY_MUSIC = ("story_music_stickers[0].music_asset_info.progressive_download_url || "
               "music_metadata.music_info.music_asset_info.progressive_download_url")
LOGIN_HINT = "run flnd login ins"


class Instagram(Plugin):
    """Instagram profiles, single posts and the login's follows, all read from the pages."""

    code = "ins"
    name = "Instagram"
    domains = ("instagram.com",)
    api = 2
    # Logged in when `sessionid` is there. Not required: a public profile shows something.
    login = cookies("sessionid", url=f"{SITE}/accounts/login/")
    browser = Browser.ANY
    # A throttled session is sent to the login wall or a challenge, which lifts after a wait;
    # a suspended or locked account is sent to its own page (2026-11).
    walls = ("/accounts/login", "/challenge", "/accounts/suspended")
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
        """A hold by the block rules for Instagram's "please wait" answer; None for the rest."""
        return 0.0 if throttled(_json(response)) else None

    # `post` comes before `user`, so instagram.com/p/<code> is never the user "p". A post's code is
    # base64url, so it takes `-` as well as `\w`.
    @route(
        r"instagram.com/p/{code:[\w-]+}", r"instagram.com/reel/{code:[\w-]+}",
        r"instagram.com/reels/{code:[\w-]+}", r"instagram.com/tv/{code:[\w-]+}",
        r"instagram.com/*/p/{code:[\w-]+}", r"instagram.com/*/reel/{code:[\w-]+}",
        r"instagram.com/*/reels/{code:[\w-]+}", r"instagram.com/*/tv/{code:[\w-]+}",
        single=True,
    )
    async def post(self, ctx: Ctx, code: str) -> AsyncIterator[Post]:
        """One post or reel, from its page: `https://www.instagram.com/p/C1abcDEFghi/`.

        Raises:
            NotFound: the page holds no post of that code, or one without files.
        """
        media = await opened_post(ctx, code)
        if not post_files(media):
            raise NotFound(f"ins: post {code} has no files")
        yield post_of(media)

    @route("ins:{me:me}", shown="ins:me", discovers=True)
    async def me(self, ctx: Ctx, me: str) -> AsyncIterator[Target]:
        """Every account the login follows, each as its own target: `ins:me`.

        Raises:
            LoginError: no login, so no account to read.
            Changed: a page of the list without its `users`.
        """
        user_id = account_id(ctx.cookies)
        if user_id is None:
            raise LoginError("ins: not logged in, so there is no account to read",
                             hint=LOGIN_HINT)
        first = Request(f"{API}/friendships/{user_id}/following/",
                        params={"count": FOLLOWING_COUNT})
        async for response in ctx.pages(first, cursor="next_max_id", send="max_id",
                                        in_page=True):
            # Instagram rotates this claim and expects it echoed back on the next calls.
            self.claim = response.headers.get("x-ig-set-www-claim") or self.claim
            data = response.json
            users = data.get("users") if isinstance(data, dict) else None
            if not isinstance(users, list):
                raise Changed("ins:me: Instagram's following list has no users")
            for user in users:
                username = user.get("username") if isinstance(user, dict) else None
                if isinstance(username, str) and username:
                    yield Target(f"ins:{username}")

    @route("ins:{user}", "instagram.com/{user}")
    async def user(self, ctx: Ctx, user: str) -> Profile:
        """A profile: `ins:example_user`, or `https://www.instagram.com/example_user/`.

        Nothing is loaded yet: a tab, or the first category that needs the profile's page,
        loads it.
        """
        return Profile(user)

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

    @user.category(own=False, url=f"{SITE}/{{user}}/reposts/")
    async def reposts(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """The posts the profile reposted, newest first."""
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
        for highlight, title in profile.highlights:
            url = f"{SITE}/stories/highlights/{highlight}/"
            for post in await story_page(ctx, url, f"highlights/{title}"):
                yield post.replace(album=title)

    @user.category(newest_first=False, folder="profile", key="{path}")
    async def profile(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """The profile picture, at its largest, under the CDN's own name; keyed by path."""
        await profile.describe(ctx)
        if profile.picture:
            picture = File(profile.picture, name=picture_name(profile.picture))
            yield Post(profile.user, [picture], headers=CDN_HEADERS)


def throttled(result: Any) -> bool:
    """Whether a JSON answer is Instagram asking to slow down instead of a result."""
    return isinstance(result, dict) and THROTTLED in str(result.get("message", ""))


def _json(response: Response) -> Any:
    """A response's JSON object; None for any other body, which isn't parsed."""
    # Only a body that starts as an object can be the throttle answer; a page is never parsed.
    if response.content[:1] != b"{":
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

    def __init__(self, user: str) -> None:
        self.user = user
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
        """Note the profile's picture and highlights from one result of its pages."""
        if not isinstance(result, dict):
            return
        # Only the profile's first result describes its user; later pages may describe others.
        if not self.described and isinstance(dig(result, "data.user"), dict):
            self.described = True
            picture = dig(result, "data.user.hd_profile_pic_url_info.url")
            self.picture = picture if isinstance(picture, str) else self.picture
        self.highlights += [h for h in listed_highlights(result) if h not in self.highlights]


async def page_results(ctx: Ctx, url: str, *,
                       enough: Callable[[list[Any]], bool] | None = None) -> list[Any]:
    """Every result of a page opened without a scroll: the ones it embeds, then its calls'.

    A "please wait" answer among them holds, and the page is opened again, so a throttle is
    never read as a page without a post or a profile without highlights.

    Args:
        enough: whether the results hold what the caller wants, a throttle beside them or
            not; None wants every answer unthrottled.
    """
    while True:
        async with aclosing(ctx.load(url, GRAPHQL, scroll=False)) as listing:
            items = [item async for item in listing]
        found = [*items[0], *items[1:]] if items else []
        if (enough is not None and enough(found)) or not any(map(throttled, found)):
            return found
        await ctx.hold(THROTTLE_REASON)


async def read_tab(ctx: Ctx, profile: Profile, tab: str) -> AsyncIterator[Post]:
    """The posts of one tab in its order, scrolling it until its `page_info` says it ended.

    A grid item that is the whole post comes with its files; any other is a lazy post, whose
    files are its page's, read only once the engine wants it. Every result the page embeds is
    read before the end is taken, as the profile's highlights may come after the last page.

    Raises:
        NotFound, LoginError: the tab's page didn't load.
    """
    reader = TAB_PAGES[tab]
    listing = ctx.load(f"{SITE}/{profile.user}/{TABS[tab]}", GRAPHQL, cursor=reader)
    async with aclosing(listing):
        embedded = True
        async for item in listing:
            ended = False
            for result in item if embedded else [item]:
                if throttled(result):
                    await ctx.hold(THROTTLE_REASON)
                    continue
                profile.note(result)
                for post in new_posts(ctx, profile, result, tab):
                    yield post
                state = reader(result)
                ended = ended or (state is not None and not (state[0] and state[1]))
            embedded = False
            # The last page, embedded or scrolled: nothing more is asked for.
            if ended:
                return


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
            yield lazy_post(ctx, code, media_pk(media)).replace(pinned=pinned)
        elif post_files(media):
            yield post_of(media).replace(pinned=pinned)


def lazy_post(ctx: Ctx, code: str, pk: str | None) -> Post:
    """A grid post dated by its pk, whose files are read from its page if the engine wants it.

    The pk's date lets the engine skip it by `--when` before any request; its files are named
    from the post's own `taken_at`. Without a pk it is listed undated.
    """

    async def files() -> list[File]:
        media = await post_page(ctx, code)
        if media.get("taken_at") not in ctx.when:
            return []
        return files_of(media, named=True)

    return Post(code, files, date=pk_stamp(pk), headers=CDN_HEADERS)


# ---------------------------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------------------------

async def post_page(ctx: Ctx, code: str) -> dict:
    """A post, from its page's HTML fetched in the logged-in tab, else from its own tab.

    The fetch never navigates a tab, so a tab being scrolled keeps its place.

    Raises:
        NotFound: neither holds a post of that code.
    """
    response = await ctx.fetch(f"{SITE}/p/{code}/", in_page=True)
    found = next((media for result in meta(response.html)
                  if (media := post_media(result, code)) is not None), None)
    return found if found is not None else await opened_post(ctx, code)


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
    landed = await ctx.open(url)
    if landed.status >= 400:
        # Raised, not skipped: a page that ended here would be recorded complete.
        raise SiteError(f"{where}: the page didn't load (HTTP {landed.status})",
                        status=landed.status)
    stories = [story for result in meta(await ctx.document()) for story in story_items(result)]
    ctx.log("info", "{where}: {n} {unit}", where=where, n=len(stories),
            unit="story" if len(stories) == 1 else "stories")
    return [post for story in stories if (post := story_post(story)) is not None]


# ---------------------------------------------------------------------------------------------
# Posts and their files
# ---------------------------------------------------------------------------------------------

def post_of(media: dict) -> Post:
    """A post with its files read: the engine reads `taken_at` as its date."""
    return Post(post_name(media), files_of(media), date=media.get("taken_at"),
                headers=CDN_HEADERS)


def files_of(media: dict, *, named: bool = False) -> list[File]:
    """Each file of a post, named here only where the templates would name it otherwise.

    Args:
        named: name every file here from the post's `taken_at`, as
            `<date>_<code>[_<n>].<extension>`: a lazy post is dated by its pk, which may be
            another day.
    """
    found = post_files(media)
    code, taken = post_name(media), utc_day(media.get("taken_at"))
    files = []
    for n, (urls, extension, audio) in enumerate(found):
        stem = f"{code}_{n}" if len(found) > 1 else code
        # A URL with another extension, or none, is named so saved names stay as they were.
        own = named or PurePosixPath(urlsplit(urls[0]).path).suffix.lower() != f".{extension}"
        name = file_name(taken, stem, extension) if own else None
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
    return Post(story_id, files, date=story.get("taken_at"), headers=CDN_HEADERS)


def file_name(taken: str | None, stem: str, extension: str) -> str:
    """A file's name: `2026-01-02_C1abc_0.jpg`, or `C1abc_0.jpg` for a post without a day."""
    return f"{taken}_{stem}.{extension}" if taken else f"{stem}.{extension}"


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
        return bool(children) and all(full_size(child) for child in children)
    if kind == "clips":
        return bool(media.get("video_versions") or media.get("video_dash_manifest"))
    if kind == "feed":
        return all(full_size(child) for child in media.get("carousel_media") or [media])
    return False


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


def _streams(versions: Any) -> list[Stream]:
    """An item's image or video versions as streams, each with its URL and size."""
    if not isinstance(versions, list):
        return []
    return [Stream(version["url"], height=version.get("height") or 0,
                   width=version.get("width") or 0)
            for version in versions if isinstance(version, dict) and version.get("url")]

