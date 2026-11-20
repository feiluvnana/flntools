"""Facebook: a profile's tabs, stories and pictures, and one video, photo or post by its link.

SPEC.md#fbk-facebook explains it: the pages it reads, the quirks, the rules a change keeps.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlsplit

from flnd.plugin import (
    Browser,
    Ctx,
    File,
    LoginError,
    NotFound,
    Plugin,
    Post,
    Response,
    SiteError,
    Target,
    UsageError,
    cookies,
    dig,
    meta,
    route,
)

SITE = "https://www.facebook.com"
# What a link Facebook has no route for here is refused with: a profile or page is what is read.
REFUSED = "{} aren't supported; give a profile or page, or a video, photo or post link"
# The CDN refuses a request without the site's Referer.
CDN_HEADERS = {"Referer": f"{SITE}/"}
# The site's own pages, which a user-name pattern must never read as a profile's name.
NOT_NAMES = (
    r"profile\.php", "people", "share", "watch", "reel", "reels", "videos", "photo",
    r"photo\.php", r"permalink\.php", r"story\.php", "login", r"login\.php",
)
USER_NAME = r"(?!(?:" + "|".join(NOT_NAMES) + r")$)[\w.]+"
# A post page's path: `<owner>/posts/<id>` (the owner's vanity name or id; a story id or `pfbid…`).
POST_LINK = re.compile(r"([A-Za-z0-9.]+)/posts/(\w+)")
# The pages that name one post by `story_fbid=` and its owner's `id=`, which `resolve` reads.
PERMALINKS = ("permalink.php", "story.php")
# The pages that name one photo by `fbid=`, which the `photo` route reads.
PHOTO_PAGES = ("photo.php", "photo")


class Tab(NamedTuple):
    """A profile tab: its category, and the paths the profile header may give it, in order."""

    category: str
    paths: tuple[str, ...]


# 2026-11-11: a tab's path differs by profile (`photos_by` or `photos`); the header's is used.
# The posts tab is the profile page itself, which the route opens.
TABS = (
    Tab("photos", ("photos_by", "photos")),
    Tab("tagged", ("photos_of",)),
    Tab("albums", ("photos_albums",)),
    Tab("reels", ("reels_tab", "reels")),
    Tab("videos", ("videos_by", "videos")),
)
# Where the profile header's user sits in the profile page's results.
HEADER = "data.user.profile_header_renderer.user"
# A story page's cards: each is one story, with its media under `attachments`.
STORY_CARDS = "data.bucket.unified_stories_with_notes.edges[].node"
# 2026-11-11: a video's progressive files moved under its delivery fragment; older pages keep
# them on the node.
PROGRESSIVE = (
    "videoDeliveryResponseFragment.videoDeliveryResponseResult.progressive_urls"
    " || progressive_urls"
)
MANIFEST = (
    "videoDeliveryResponseFragment.videoDeliveryResponseResult.dash_manifests[0].manifest_xml"
    " || dash_manifests[0].manifest_xml || dash_manifest"
)
# Video URL fallbacks, best first, for a node with neither DASH nor progressive files.
VIDEO_URLS = (
    "browser_native_hd_url", "playable_url_quality_hd", "browser_native_sd_url", "playable_url",
)
PHOTO_URL = "viewer_image.uri || large_share_image.uri || photo_image.uri || image.uri"
# 2026-11-11: a grid tile holds a small copy of a photo its inner node has in full.
TILE = "TimelineAppCollectionItem"
# The header's cover and pictures are the profile category's, not any tab's.
NOT_MEDIA = {"profile_header_renderer"}
# Date fields in order of preference; `posted_on` takes the first that holds a Unix time.
TIMESTAMPS = ("creation_time", "publish_time", "post_timestamp", "timestamp", "created_time")
# The Comet GraphQL endpoint's responses carry the feed.
FEED = "graphql"
# A video's id in the URL a video link lands on: `/videos/<id>`, `/<page>/videos/<id>`,
# `/reel/<id>`.
VIDEO_PATH = re.compile(r"/(?:videos|reel|reels)/(?:[^/]+/)?(\d+)")
# 2026-11-12: a throttled session gets a page saying this, or a GraphQL line that is Comet's
# error page.
BLOCKED = "temporarily blocked"
COMET_ERROR = "CometErrorRoot.react"
TEMPORARILY_BLOCKED = "Facebook says you've been temporarily blocked"
# Comet's rate-limit codes in a GraphQL line's `errors` (confirm live: the set is a guess).
RATE_LIMIT_CODES = frozenset({"1675004", "368"})
# 2026-11-12: a comment list carries its own `page_info`, deeper than the tab's; the tab's sits
# at most this many objects under `data` (data, node, connection, page_info).
PAGE_INFO_DEPTH = 3
# A media node's owner by vanity name (unconfirmed live).
OWNER_NAME = "owner.vanity || video_owner.vanity"


class Facebook(Plugin):
    """Facebook profiles and single videos, photos and posts, read in a logged-in browser."""

    code = "fbk"
    name = "Facebook"
    domains = ("facebook.com", "fb.com", "fb.watch")
    api = 2
    # Images, media and fonts load until a live check shows its pages work without them.
    hidden = True
    # Not required: a logged-out browser gets what Facebook shows the public, which is little.
    login = cookies("c_user", "xs", url=f"{SITE}/login/")
    # No published limit; the floor only keeps a burst slower than the other sites'.
    pace = 0.2
    browser = Browser.ANY
    walls = ("/login", "/login.php")
    # A file's folder is where it was found: `part` is its tab (or "videos" or "albums"),
    # `album` the album's title. A single link's files have neither.
    folder = "{part}/{album}"
    fields = ("part", "album")
    filename = "{date}_{post}"
    # A photo found in two tabs is saved once; changing it downloads every saved file again.
    key = "{post}"
    # Refused before any route that has fewer literal parts: `watch?v=<id>` still reads.
    unsupported = {
        f"{host}/{path}": REFUSED.format(what) for host in ("facebook.com", "fb.com")
        for path, what in (("photos", "photo lists"), ("groups", "groups"), ("events", "events"),
                           ("stories", "stories"), ("marketplace", "marketplace pages"),
                           ("hashtag", "hashtag pages"), ("watch", "watch pages without v="))
    }

    @classmethod
    def resolve(cls, text: str) -> Target | None:
        """Read a `permalink.php` or `story.php` link as a post; anything else by the routes.

        Those links name a post by `story_fbid=` and its owner's `id=`, which no pattern
        captures together; the target's value is `<id>_<story_fbid>`.

        Raises:
            UsageError: a permalink lacks one of the two values, a photo link its `fbid=`, or a
                name is one of the site's own pages.
        """
        written = text.strip()
        if written[:4].lower() == "fbk:":
            written = written[4:]
        link = written if written.lower().startswith(("http://", "https://")) else (
            f"https://{written}")
        parts = urlsplit(link)
        host = (parts.hostname or "").lower()
        page = parts.path.strip("/")
        on_site = any(host == d or host.endswith("." + d) for d in ("facebook.com", "fb.com"))
        if "/" not in written and not written.lower().startswith("http"):
            if re.fullmatch("|".join(NOT_NAMES), written.lstrip("@"), re.IGNORECASE):
                raise UsageError(f"fbk: {written!r} is a page of Facebook, not a profile; "
                                 "give the whole link")
            return super().resolve(text)
        if on_site and page in PERMALINKS:
            query = parse_qs(parts.query)
            story = (query.get("story_fbid") or [""])[0]
            owner = (query.get("id") or [""])[0]
            if not re.fullmatch(r"\w+", story) or not owner.isdigit():
                raise UsageError(f"fbk: a {page} link names its post by story_fbid= and id=; "
                                 "this one lacks one of them")
            return Target(f"fbk:post/{owner}_{story}", kind="post")
        if on_site and page in PHOTO_PAGES:
            fbid = (parse_qs(parts.query).get("fbid") or [""])[0]
            if not fbid.isdigit():
                raise UsageError(f"fbk: a {page} link names its photo by fbid=<number>; "
                                 "this one has none")
        return super().resolve(text)

    def blocked(self, response: Response) -> float | None:
        """Hold by the block rules for Facebook's block page, which embeds no results."""
        text = response.text
        if "__bbox" not in text and BLOCKED in text.lower():
            return 0.0
        return None

    @route(*((pattern, "video/{video}") for pattern in (
        r"fbk:video/{video:\d+}",
        r"facebook.com/watch?v={video:\d+}", r"fb.com/watch?v={video:\d+}",
        r"facebook.com/*/videos/{video:\d+}", r"fb.com/*/videos/{video:\d+}",
        r"facebook.com/*/videos/*/{video:\d+}", r"fb.com/*/videos/*/{video:\d+}",
        r"facebook.com/videos/{video:\d+}", r"fb.com/videos/{video:\d+}",
        r"facebook.com/reel/{video:\d+}", r"fb.com/reel/{video:\d+}",
        r"facebook.com/reels/{video:\d+}", r"fb.com/reels/{video:\d+}",
    )), single=True)
    async def video(self, ctx: Ctx, video: str) -> AsyncIterator[Post]:
        """One video or reel: `fbk:video/<id>`, or its link, `facebook.com/watch/?v=<id>`."""
        video = video.removeprefix("video/")
        async for post in read_single(ctx, f"{SITE}/watch/?v={video}", wanted=video):
            yield post

    @route((r"fb.watch/{code:[\w-]+}", "watch/{code}"), single=True)
    async def watch(self, ctx: Ctx, code: str) -> AsyncIterator[Post]:
        """One video by its `fb.watch` short link, followed in the logged-in browser.

        The video's id is read from where the link lands, so the same video by its long link
        has the same key. A landing on the login page is the engine's wall.

        Raises:
            NotFound: the link didn't lead to a video or a post.
        """
        async for post in follow(ctx, f"https://fb.watch/{code.removeprefix('watch/')}/"):
            yield post

    @route((r"facebook.com/share/{kind:[prv]}/{code:[\w-]+}", "share/{kind}/{code}"),
           (r"fb.com/share/{kind:[prv]}/{code:[\w-]+}", "share/{kind}/{code}"), single=True)
    async def share(self, ctx: Ctx, share: str) -> AsyncIterator[Post]:
        """One post, reel or video by its `/share/p|r|v/<code>` link, followed like `watch`.

        Raises:
            NotFound: the link didn't lead to a video or a post.
        """
        async for post in follow(ctx, f"{SITE}/{share}/"):
            yield post

    @route(*((pattern, "photo/{photo}") for pattern in (
        r"fbk:photo/{photo:\d+}",
        r"facebook.com/photo.php?fbid={photo:\d+}", r"fb.com/photo.php?fbid={photo:\d+}",
        r"facebook.com/photo?fbid={photo:\d+}", r"fb.com/photo?fbid={photo:\d+}",
    )), single=True)
    async def photo(self, ctx: Ctx, photo: str) -> AsyncIterator[Post]:
        """One photo: `fbk:photo/<id>`, or its `photo.php?fbid=<id>` or `photo/?fbid=<id>` link."""
        photo = photo.removeprefix("photo/")
        async for post in read_single(ctx, f"{SITE}/photo/?fbid={photo}", wanted=photo):
            yield post

    @route(
        (r"fbk:post/{user:[A-Za-z0-9.]+}_{post:\w+}", "post/{user}_{post}"),
        (r"facebook.com/{user:[A-Za-z0-9.]+}/posts/{post:\w+}", "post/{user}_{post}"),
        (r"fb.com/{user:[A-Za-z0-9.]+}/posts/{post:\w+}", "post/{user}_{post}"),
        (r"facebook.com/permalink.php?story_fbid={post:\w+}&id={user:\d+}",
         "post/{user}_{post}"),
        (r"fb.com/permalink.php?story_fbid={post:\w+}&id={user:\d+}", "post/{user}_{post}"),
        (r"facebook.com/story.php?story_fbid={post:\w+}&id={user:\d+}", "post/{user}_{post}"),
        (r"fb.com/story.php?story_fbid={post:\w+}&id={user:\d+}", "post/{user}_{post}"),
        shown=f"{SITE}/<owner>/posts/<post>",
        single=True,
    )
    async def post(self, ctx: Ctx, post: str) -> AsyncIterator[Post]:
        """One post's photos and videos its owner owns, by its permalink (`resolve`).

        Args:
            post: `<owner id or name>_<story id>`, as `resolve` made it, after `post/`.
        """
        owner, _, story = post.removeprefix("post/").partition("_")
        async for found in read_post(ctx, owner, story):
            yield found

    # Before `user`, whose `fbk:{user}` would read `fbk:me` as a name.
    @route("fbk:{me:me}", shown="fbk:me", discovers=True)
    async def me(self, ctx: Ctx, me: str) -> AsyncIterator[Target]:
        """The accounts the login follows, pages and people, each as `fbk:<name or id>`.

        The logged-in following page is scrolled with the feed the tabs use. Confirm live: the
        page (`/me/following`) and the shape of its tiles.

        Raises:
            LoginError: no login, so no account to read.
        """
        if "c_user" not in ctx.cookies:
            raise LoginError("fbk: not logged in, so there is no account to read",
                             hint="log in: flnd auth login fbk")
        seen: set[str] = set()
        async for result in tab_results(ctx, f"{SITE}/me/following", "following"):
            for name in followed(result):
                if name not in seen:
                    seen.add(name)
                    yield Target(f"fbk:{name}")

    @route(
        "fbk:{user}",
        r"facebook.com/profile.php?id={user:\d+}", r"fb.com/profile.php?id={user:\d+}",
        r"facebook.com/people/*/{user:\d+}", r"fb.com/people/*/{user:\d+}",
        r"facebook.com/@{user}", r"fb.com/@{user}",
        f"facebook.com/{{user:{USER_NAME}}}", f"fb.com/{{user:{USER_NAME}}}",
        # Live check pending: whether the tabs list newest first; until then each is read whole.
        newest_first=False,
    )
    async def user(self, ctx: Ctx, user: str) -> dict[str, Any]:
        """A profile by user name or id: `fbk:example.page`, opened once for every category.

        The GraphQL feed is watched from before the page loads, so the responses of its first
        load are kept for `posts`.

        Returns:
            The profile (`profile_of`), with each tab's URL for the categories' warm tabs.

        Raises:
            SiteError: the profile page didn't load.
        """
        url = f"{SITE}/{user}"
        async with ctx.feed(FEED, scroll=False, lines=True) as arrived:
            results = await page_results(ctx, url)
            if results is None:
                raise SiteError(f"fbk: {url} didn't load", hint="check the profile in a browser")
            early = [line async for line in arrived]
        return profile_of(url, results, early)

    # The posts tab is the profile page the route left open, so it comes first: the tabs after
    # it navigate away.
    @user.category
    async def posts(self, ctx: Ctx, profile: Mapping[str, Any]) -> AsyncIterator[Post]:
        """The profile's own posts: the page's photos and videos, then its scroll's."""
        seen: set[str] = set()
        # Not filtered by owner: the header that names the owner came from the same results.
        for result in profile["results"]:
            for post in media_posts(result, "posts", seen=seen):
                yield post
        async with ctx.feed(FEED, scroll=True, lines=True, cursor=tab_cursor(),
                            resume=True) as lines:
            async for line in unblocked(ctx, lines):
                for post in media_posts(line, "posts", owner=profile["owner"], seen=seen):
                    yield post

    @user.category(url="{photos_url}")
    async def photos(self, ctx: Ctx, profile: Mapping[str, Any]) -> AsyncIterator[Post]:
        """The photos tab."""
        async for post in read_tab(ctx, profile, "photos"):
            yield post

    @user.category(own=False, url="{tagged_url}")
    async def tagged(self, ctx: Ctx, profile: Mapping[str, Any]) -> AsyncIterator[Post]:
        """The photos the profile is tagged in."""
        async for post in read_tab(ctx, profile, "tagged"):
            yield post

    @user.category(url="{albums_url}")
    async def albums(self, ctx: Ctx, profile: Mapping[str, Any]) -> AsyncIterator[Post]:
        """The albums tab, scrolled to its end for each album's link, then each album.

        Media on the tab itself is yielded as it comes; each album's goes in
        `albums/<title>/`. The albums' pages load in warm tabs while the first is read.
        """
        url, owner = profile["albums_url"], profile["owner"]
        if not url:
            ctx.log("info", "albums: this profile has no such tab")
            return
        listed: dict[str, str] = {}
        async for result in tab_results(ctx, url, "albums"):
            if isinstance(result, dict):
                listed.update(album_links(result))
            for post in media_posts(result, "albums", owner=owner):
                yield post
        await ctx.warm(list(listed))
        for album, title in listed.items():
            async for result in tab_results(ctx, album, "album"):
                for post in media_posts(result, "albums", album=title, owner=owner):
                    yield post

    @user.category(url="{reels_url}")
    async def reels(self, ctx: Ctx, profile: Mapping[str, Any]) -> AsyncIterator[Post]:
        """The reels tab."""
        async for post in read_tab(ctx, profile, "reels"):
            yield post

    @user.category(url="{videos_url}")
    async def videos(self, ctx: Ctx, profile: Mapping[str, Any]) -> AsyncIterator[Post]:
        """The videos tab."""
        async for post in read_tab(ctx, profile, "videos"):
            yield post

    @user.category
    async def stories(self, ctx: Ctx, profile: Mapping[str, Any]) -> AsyncIterator[Post]:
        """The stories active now: each story bucket the header lists, one page each.

        A card's date dates every media it attaches.

        Raises:
            SiteError: a bucket's page didn't load.
        """
        if not profile["header"]:
            ctx.log("warn", "the profile header wasn't found; its stories are skipped")
            return
        # Live check pending: a bucket by `fetch(in_page=True)`; until then one page each,
        # loading in warm tabs while the first is read.
        urls = [f"{SITE}/stories/{bucket}/" for bucket in profile["buckets"]]
        await ctx.warm(urls)
        for url in urls:
            results = await page_results(ctx, url)
            if results is None:
                raise SiteError(f"fbk: the story bucket {url} didn't load")
            cards = [card for result in results for card in dig(result, STORY_CARDS)
                     if isinstance(card, dict)]
            for card in cards:
                stamp = posted_on(card)
                for media in dig(card, "attachments[].media"):
                    if not isinstance(media, dict):
                        continue
                    item = video_item(media, stamp) or photo_item(media, stamp)
                    if item is not None:
                        yield post_of(item, "stories")
            ctx.log("info", "stories: {n} in {url}", n=len(cards), url=url)

    # Keyed by its path, so a new picture is a new file; the name, extension included, is
    # pinned so that key stays today's.
    @user.category(folder="profile", key="{path}")
    async def profile(self, ctx: Ctx, profile: Mapping[str, Any]) -> AsyncIterator[Post]:
        """The profile picture and the cover photo, named `profile_<id>` and `cover_<id>`."""
        if not profile["header"]:
            ctx.log("warn", "the profile header wasn't found; its pictures are skipped")
            return
        for kind, url, photo_id in profile["pictures"]:
            name = f"{kind}_{photo_id}.{image_extension(url)}"
            yield Post(photo_id, [File(url, name=name)], headers=CDN_HEADERS)


# ---------------------------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------------------------

async def page_results(ctx: Ctx, url: str) -> list[Any] | None:
    """Open a page and return the GraphQL results it embeds.

    A page that embeds none but says the session is blocked is held, then opened again; the
    engine's block rules end that with `Blocked`.

    Returns:
        The results; [] for a page that embeds none; None when the tab landed elsewhere with
        none, so the page didn't load.
    """
    while True:
        landed = await ctx.open(url)
        page = await ctx.document()
        results = [found for found in meta(page) if isinstance(found, dict) and "data" in found]
        if results:
            return results
        if BLOCKED not in page.text.lower():
            return [] if _place(landed.url) == _place(url) else None
        await ctx.hold(TEMPORARILY_BLOCKED)


async def tab_results(ctx: Ctx, url: str, tab: str) -> AsyncIterator[Any]:
    """Each GraphQL result of a scrolled page: those it embeds, then its feed's to the end.

    Raises:
        SiteError: the page didn't load.
    """
    async with ctx.feed(FEED, scroll=True, lines=True, cursor=tab_cursor(), resume=True) as lines:
        results = await page_results(ctx, url)
        if results is None:
            raise SiteError(f"fbk: {url} didn't load")
        for result in results:
            yield result
        async for line in unblocked(ctx, lines):
            yield line


def held(line: Any) -> bool:
    """Whether a GraphQL line is Comet's block error or carries a rate-limit error code."""
    errors = line.get("errors") if isinstance(line, dict) else None
    return isinstance(line, dict) and (line.get("__dr") == COMET_ERROR or (
        isinstance(errors, list) and any(isinstance(error, dict) and str(error.get("code"))
                                         in RATE_LIMIT_CODES for error in errors)))


async def unblocked(ctx: Ctx, lines: AsyncIterator[Any]) -> AsyncIterator[Any]:
    """A feed's lines, holding for each one `held` names instead of yielding it."""
    async for line in lines:
        if held(line):
            await ctx.hold(TEMPORARILY_BLOCKED)
        else:
            yield line


async def read_tab(ctx: Ctx, profile: Mapping[str, Any], category: str) -> AsyncIterator[Post]:
    """The posts of one tab, as it scrolls; a media two results hold is yielded once."""
    url = profile[f"{category}_url"]
    if not url:
        ctx.log("info", "{tab}: this profile has no such tab", tab=category)
        return
    seen: set[str] = set()
    async for result in tab_results(ctx, url, category):
        for post in media_posts(result, category, owner=profile["owner"], seen=seen):
            yield post


async def read_single(ctx: Ctx, url: str, *, wanted: str | None = None, owner: str | None = None,
                      category: str | None = None) -> AsyncIterator[Post]:
    """The media of one video, photo or post page, in its owner's folder.

    A video's or photo's page also shows suggestions, so only the media whose id is `wanted`
    is kept; a post's page keeps every media `owner` owns.

    Raises:
        NotFound: the page held none of the media asked for.
    """
    async with ctx.feed(FEED, scroll=False, lines=True) as arrived:
        results = await page_results(ctx, url)
        early = [line async for line in arrived] if results is not None else []
    seen: set[str] = set()
    found = [item for result in [*(results or []), *early]
             for item in media_items(result, seen=seen, owner=owner)
             if wanted is None or item.id == wanted]
    if not found:
        raise NotFound(f"fbk: {url} shows no photo or video",
                       hint="it may be private or deleted; if you can see it, "
                            "run flnd auth login fbk")
    for item in found:
        # Its folder is its category's, found in the data; a post's page files all as `posts`.
        item = item._replace(owner=item.owner or owner)
        kind = category or post_of(item, "").category
        yield post_of(item, kind if item.owner else "", category=kind)


async def follow(ctx: Ctx, link: str) -> AsyncIterator[Post]:
    """Open a short or share link, read where it lands, then read that video or post.

    Raises:
        NotFound: it landed on neither a video nor a post.
    """
    landed = (await ctx.open(link)).url
    if (video := video_id(landed)) is not None:
        async for post in read_single(ctx, f"{SITE}/watch/?v={video}", wanted=video):
            yield post
        return
    owner, _, story = post_id(landed).partition("_")
    if not story:
        raise NotFound(f"fbk: {link} didn't lead to a video or a post",
                       hint="check the link in a browser")
    async for post in read_post(ctx, owner, story):
        yield post


def read_post(ctx: Ctx, owner: str, story: str) -> AsyncIterator[Post]:
    """One post's media its owner owns: by `permalink.php` for an owner id, else by its name."""
    url = (f"{SITE}/permalink.php?story_fbid={story}&id={owner}" if owner.isdigit()
           else f"{SITE}/{owner}/posts/{story}")
    return read_single(ctx, url, owner=owner, category="posts")


def followed(result: Any) -> Iterator[str]:
    """The name or id of each person or page a following page's result lists. Confirm live."""
    for tile in dig(result, f"..[__typename={TILE}]"):
        link = urlsplit(tile.get("url") or "")
        if dig(tile, "node.__typename") in ("User", "Page"):
            name = (parse_qs(link.query).get("id") or [link.path.strip("/").split("/")[0]])[0]
            if name:
                yield name


def _place(url: str) -> tuple[str, str]:
    """A URL's path and query, without a trailing slash: where a tab is, whatever its host."""
    parts = urlsplit(url)
    return parts.path.rstrip("/"), parts.query


def video_id(url: str) -> str | None:
    """The video id a video page's URL names (`?v=`, `/videos/<id>`, `/reel/<id>`); None for
    a URL off Facebook or one that names none."""
    parts = urlsplit(url)
    if not (parts.hostname or "").endswith(("facebook.com", "fb.com")):
        return None
    given = (parse_qs(parts.query).get("v") or [""])[0]
    if given.isdigit():
        return given
    found = VIDEO_PATH.search(parts.path)
    return found[1] if found else None


def post_id(url: str) -> str:
    """`<owner>_<story id>` for a post page's URL (`permalink.php`, `/<owner>/posts/<id>`);
    "" for a URL off Facebook or one that names none."""
    parts = urlsplit(url)
    if not (parts.hostname or "").endswith(("facebook.com", "fb.com")):
        return ""
    query = parse_qs(parts.query)
    story, owner = (query.get("story_fbid") or [""])[0], (query.get("id") or [""])[0]
    if story and owner.isdigit():
        return f"{owner}_{story}"
    found = POST_LINK.fullmatch(parts.path.strip("/"))
    return f"{found[1]}_{found[2]}" if found else ""


def profile_of(url: str, results: list[Any], early: list[Any]) -> dict[str, Any]:
    """What every category of a profile reads, from its page's results and early responses.

    Returns:
        `url`; `results` (the page's and its first load's, the posts tab's first batch);
        `header` (whether it was found; a page's has no `tagged` tab); `owner` (the profile's
        id, which keeps other accounts' media out of its tabs; None keeps all); `pictures` as
        (kind, URL, id); `buckets` (story bucket ids); and `<tab>_url` per tab, "" when the
        header lacks it. Without a header every tab is tried at its first path.
    """
    header = next((found for result in results if (found := read_header(result)) is not None),
                  None)
    paths = header["paths"] if header else set()
    profile = {
        "url": url, "results": [*results, *early], "header": header is not None,
        "owner": header["id"] if header else None,
        "pictures": header["pictures"] if header else [],
        "buckets": header["buckets"] if header else [],
    }
    for tab in TABS:
        path = None if header and header["page"] and tab.category == "tagged" else (
            tab_path(tab, paths))
        profile[f"{tab.category}_url"] = f"{url}/{path}" if path else ""
    return profile


def tab_path(tab: Tab, paths: set[str]) -> str | None:
    """A tab's path on this profile: the first the header names, or its first without one."""
    if not paths:
        return tab.paths[0]
    return next((path for path in tab.paths if path in paths), None)


def read_header(result: Any) -> dict[str, Any] | None:
    """The profile header's id, pictures, story buckets and tab paths; None without one.

    The tab paths are the `tab_key` of each navigation section, and of each collection in a
    section: the photos tab, for one, is a collection of its section. A picture without its
    photo id is named by "picture" or "cover" instead.
    """
    user = dig(result, HEADER)
    if not isinstance(user, dict):
        return None
    sections = [section for section in dig(
        user, "profile_tabs.profile_user.timeline_nav_app_sections.edges[].node")
        if isinstance(section, dict)]
    paths = {section.get("tab_key") for section in sections} | {
        collection.get("tab_key") for section in sections
        for collection in dig(section, "all_collections.nodes") or []
        if isinstance(collection, dict)
    }
    pictures = [
        (kind, url, dig(user, id_path) or kind)
        for kind, url, id_path in (
            ("profile", dig(user, "profilePicLarge.uri"), "profilePhoto.id"),
            ("cover", dig(user, "cover_photo.photo.image.uri"), "cover_photo.photo.id"))
        if url
    ]
    buckets = [str(node["id"]) for node in dig(user, "story_bucket.nodes") or []
               if isinstance(node, dict) and node.get("id")]
    # A page's `__typename` is "Page" (confirm live): it has no tagged tab.
    return {"id": user.get("id"), "pictures": pictures, "buckets": buckets,
            "paths": {path for path in paths if path}, "page": user.get("__typename") == "Page"}


# ---------------------------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------------------------

def next_page(line: Any) -> tuple[bool, str | None] | None:
    """A result line's `(has a next page, cursor)`, or None when it says nothing about paging.

    The shallowest `page_info` at most `PAGE_INFO_DEPTH` objects under `data`, never inside a
    list, counts; a comment list's sits deeper. `flnd.plugin.page_info` reads one fixed path,
    which the tab's isn't.
    """
    level = [line.get("data")] if isinstance(line, dict) else []
    for _ in range(PAGE_INFO_DEPTH + 1):
        nested = []
        for node in level:
            if not isinstance(node, dict):
                continue
            info = node.get("page_info")
            if isinstance(info, dict) and ("end_cursor" in info or "has_next_page" in info):
                cursor = info.get("end_cursor")
                return bool(info.get("has_next_page") and cursor), str(cursor) if cursor else None
            nested += [value for value in node.values() if isinstance(value, dict)]
        level = nested
    return None


def tab_cursor() -> Callable[[Any], tuple[bool, str | None] | None]:
    """A cursor reader for one scrolled tab: `next_page`, but a last page ends the feed only
    after some page said there was a next one.

    2026-11, live: on some loads the profile's Tiles feed answers first with no next page,
    which says nothing about the tab.
    """
    seen_more = False

    def read(line: Any) -> tuple[bool, str | None] | None:
        nonlocal seen_more
        state = next_page(line)
        if state is None:
            return None
        if state[0]:
            seen_more = True
            return state
        return state if seen_more else None

    return read


# ---------------------------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------------------------

class Item(NamedTuple):
    """One photo or video found in a result."""

    # Its id, which names its file and is its key in the history.
    id: str
    # A DASH video's video stream, an image, or a progressive video's every address, HD first.
    url: str | tuple[str, ...]
    # "image" or "video".
    kind: str
    # When it was posted, as the site gives it (Unix seconds).
    date: Any
    # The album's title, when the photo sits in one.
    album: str | None = None
    # A DASH video's audio stream, joined with it.
    audio: str | None = None
    # Its owner's vanity name, else id (None: unnamed); and whether its node's type says reel.
    owner: str | None = None
    reel: bool = False


def media_posts(result: Any, tab: str, *, album: str | None = None, owner: str | None = None,
                seen: set[str] | None = None) -> Iterator[Post]:
    """Each photo and video of `owner` in one result of a tab, filed where it belongs.

    A photo that names its album goes under `albums/<album>/`, and a video found in a tab
    other than reels or videos under `videos/`.

    Args:
        tab: the category of the tab being read.
        album: the album being read, which files every media under it.
        owner: the profile's id or name: media another account owns is left out. None keeps all.
        seen: the ids already yielded across one tab; None starts a set for this result.
    """
    if not isinstance(result, dict):
        return
    for item in media_items(result, seen=seen, owner=owner):
        if item.album or album:
            part, title = "albums", item.album or album
        elif item.kind == "video" and tab not in ("reels", "videos"):
            part, title = "videos", None
        else:
            part, title = tab, None
        yield post_of(item, part, album=title)


def post_of(item: Item, part: str, *, album: str | None = None,
            category: str | None = None) -> Post:
    """One item as a post of one file, with the CDN's Referer; the engine names its extension
    from the content (the CDN converts an image to the format its `stp=dst-<format>` names).

    Its `owner` and `category` are read only for a single link."""
    if category is None:
        category = "photos" if item.kind == "image" else "reels" if item.reel else "videos"
    if item.audio is not None or isinstance(item.url, str):
        file = File(item.url, audio=item.audio)
    else:
        file = File(mirrors=item.url)
    return Post(item.id, [file], date=item.date, headers=CDN_HEADERS, owner=item.owner,
                category=category, part=part, album=album)


def image_extension(url: str) -> str:
    """An image's extension, without a dot: its `stp=dst-<format>` parameter's, else its path's
    when that is an image's, else "jpg". 2026-11-11: the CDN converts to the format it names."""
    parts = urlsplit(url)
    converted = re.search(r"dst-(jpg|png|webp|gif)", parse_qs(parts.query).get("stp", [""])[0])
    if converted:
        return converted[1]
    suffix = parts.path.rsplit("/", 1)[-1].rpartition(".")[2].lower()
    return suffix if suffix in ("jpg", "png", "webp", "gif") else "jpg"


def album_links(data: Any) -> dict[str, str]:
    """The albums a result lists, as {url: title}: each `Album` tile with a link."""
    return {
        tile["url"]: dig(tile, "title.text") or "album"
        for tile in dig(data, f"..[__typename={TILE}]")
        if dig(tile, "node.__typename") == "Album" and tile.get("url")
    }


def media_items(data: Any, stamp: Any = None, seen: set[str] | None = None, *,
                owner: str | None = None) -> Iterator[Item]:
    """Every photo and video in a result, each once, dated by its nearest dated node.

    A video's node isn't searched further (its thumbnails are no photos), nor is the profile
    header. 2026-11-11: other accounts' media (a comment's photo) shows in a profile's tabs,
    so media whose owner id isn't `owner` is left out.

    Args:
        stamp: the date of the nearest dated node above `data`.
        seen: the ids found so far, shared across results.
        owner: the profile's id or name; None keeps all.
    """
    seen = set() if seen is None else seen
    if isinstance(data, list):
        for value in data:
            yield from media_items(value, stamp, seen, owner=owner)
        return
    if not isinstance(data, dict):
        return
    stamp = posted_on(data) or stamp
    item = video_item(data, stamp) or photo_item(data, stamp)
    owned_by, named = dig(data, "owner.id || video_owner.id"), dig(data, OWNER_NAME)
    # An owner written as a name is compared with the node's vanity name, when it has one.
    theirs = owned_by if (owner or "0").isdigit() else named
    if item is not None and item.id not in seen and not (owner and theirs and theirs != owner):
        seen.add(item.id)
        yield item._replace(owner=named or owned_by)
    if item is not None and item.kind == "video":
        return
    for key, value in data.items():
        if key not in NOT_MEDIA:
            yield from media_items(value, stamp, seen, owner=owner)


def video_item(node: dict, stamp: Any) -> Item | None:
    """A video node's best file: its DASH streams, else its progressive files and URLs as
    mirrors, HD first; None for a node that isn't a video or has no id or no file."""
    typename = node.get("__typename", "")
    if not ("Video" in typename or "Reel" in typename or "progressive_urls" in node):
        return None
    video = str(node.get("id") or node.get("video_id") or "")
    reel = "Reel" in typename
    if video and (joined := File.dash(dig(node, MANIFEST))) is not None:
        return Item(video, joined.url or "", "video", stamp, audio=joined.audio, reel=reel)
    progressive = [entry for entry in dig(node, PROGRESSIVE) or [] if isinstance(entry, dict)]
    hd = [entry for entry in progressive if dig(entry, "metadata.quality") == "HD"]
    found = ([entry.get("progressive_url") for entry in hd + progressive]
             + [node.get(key) for key in VIDEO_URLS])
    urls = tuple(dict.fromkeys(url for url in found if isinstance(url, str) and url))
    if not video or not urls:
        return None
    return Item(video, urls[0] if len(urls) == 1 else urls, "video", stamp, reel=reel)


def photo_item(node: dict, stamp: Any) -> Item | None:
    """A photo node's largest image, with its album's name; None for a grid tile, a node that
    isn't a photo, or one with no id or no image URL."""
    if node.get("__typename") == TILE:
        return None
    has_image = isinstance(node.get("image"), dict) and "uri" in node["image"]
    if not ("Photo" in node.get("__typename", "") or has_image
            or any(key in node for key in ("large_share_image", "viewer_image", "photo_image"))):
        return None
    photo = str(node.get("id") or node.get("photo_id") or node.get("legacy_photo_id") or "")
    url = dig(node, PHOTO_URL)
    if not photo or not isinstance(url, str) or not url:
        return None
    return Item(photo, url, "image", stamp, dig(node, "album.name"))


def posted_on(node: dict) -> Any:
    """A node's own date: the first of `TIMESTAMPS` above 0, a number or its digits as text;
    None for none (Facebook gives 0 for undated media)."""
    for key in TIMESTAMPS:
        value = node.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit() and int(value) > 0:
            return value
    return None
