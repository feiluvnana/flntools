"""TikTok on plugin API 2: a profile read from its own page's list answers in a tab, and one
post from its public embed page over HTTP. SPEC.md#tik-tiktok explains it.
"""

from __future__ import annotations

import datetime
import re
import sys
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote_plus, urlsplit

from flnd.plugin import (
    Blocked,
    Browser,
    Changed,
    Ctx,
    Document,
    File,
    Hidden,
    LoginError,
    NetworkError,
    NotFound,
    Plugin,
    Post,
    Request,
    SiteError,
    Stream,
    Target,
    best,
    cookies,
    dig,
    route,
)

# The user agent TikTok's embed page and CDN have always been sent: a desktop Chrome.
TIKTOK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/128.0.0.0 Safari/537.36")
SITE = "https://www.tiktok.com"
# A captcha's containers (TikTok has used each of these ids and classes, seen 2026-11-11), and
# the words it shows.
CAPTCHA_CSS = ('#captcha-verify-container-main-page, #captcha_container, [id*="captcha-verify"], '
               '[class*="captcha_verify"], [class*="captcha-verify"]')
CAPTCHA_TEXT = re.compile(
    r"Drag the slider|Drag the puzzle|Verify to continue|Select 2 objects", re.IGNORECASE,
)
# The links to a profile's posts, read from the page when its list is walled.
POST_LINKS = 'a[href*="/video/"], a[href*="/photo/"]'
POST_LINK = re.compile(r"/(?:video|photo)/(\d+)")
# Seconds to let the profile settle before looking for a captcha: it shows a few seconds after
# the load event. The value is a margin over that; where it was measured isn't recorded.
SETTLE = 5.0
# Seconds a tab (Reposts, Liked, Favorites) gets to appear and take a click.
CLICK_TIMEOUT = 10.0
# Seconds a person gets to solve a captcha before it is held as a block.
CAPTCHA_TIMEOUT = 60.0
# The times the post list is read again after it stopped short under a captcha.
REREADS = 3
# The profile's data, server-rendered into the page: the script's id, and the keys to the user
# under it. "webapp.user-detail" is one key with a dot in it, so a `dig` path can't name it.
PAGE_DATA = "__UNIVERSAL_DATA_FOR_REHYDRATION__"
USER_KEYS = ("__DEFAULT_SCOPE__", "webapp.user-detail", "userInfo")
# The earliest Unix time taken as a post's date: 2004-01-01 UTC, the engine's first year. A
# smaller `createTime` (0, a small number) is a placeholder, and the id is read instead. A time
# more than a day past the clock is a mistake for another number.
EARLIEST = 1_072_915_200
SKEW = 86_400
# The CDN cookies, in the order they are sent: the chain token TikTok sets for every visitor,
# and the login's session.
CDN_COOKIES = ("tt_chain_token", "sessionid")
# The API paths whose answers fill each list. Liked is served from `favorite` and Favorites
# from `collect`: the names are swapped on TikTok's side (2026-11-11).
FEEDS = {
    "videos": ("/api/post/item_list",),
    "stories": ("/api/story/item_list", "/api/user/story"),
    "reposts": ("/api/repost/item_list",),
    "liked": ("/api/favorite/item_list",),
    "favorites": ("/api/user/collect/item_list",),
}
# The search page's answers, and the Following list's (both unconfirmed live, 2026-11-20).
SEARCH_API = "/api/search/item/full/"
FOLLOWING_API = "/api/user/list/"
# The logged-in user's own profile (it redirects to `/@<name>`), and the click that opens its
# Following list: unconfirmed live.
OWN_PAGE = f"{SITE}/profile"
FOLLOWING_TAB = '[data-e2e="following"]'
# Each clicked tab's `data-e2e` name.
TABS = {"reposts": "repost-tab", "liked": "liked-tab", "favorites": "favorites-tab"}


def _wanted_categories(target: Any = None) -> set[str] | None:
    """The categories wanted for this target, if known."""
    if target is not None and hasattr(target, "text") and "|" in target.text:
        tail = target.text.rpartition("|")[2]
        tokens = {t.strip() for t in tail.split(",") if t.strip()}
        if "all" in tokens:
            return None
        if tokens:
            return tokens
    frame = sys._getframe()
    while frame:
        if "categories" in frame.f_locals:
            cats = frame.f_locals["categories"]
            if isinstance(cats, (list, tuple)):
                names = {c.name if hasattr(c, "name") else c for c in cats}
                found = {n for n in names if isinstance(n, str)}
                if "all" in found:
                    return None
                if found:
                    return found
        frame = frame.f_back
    return None


class Profile:
    """One profile's page, shared by the target's categories, opened on first need.

    Every open listens for the stories, which come with the load and need no scroll. Only
    `videos` and `playlists` watch the post list, scrolled once for both: each item waits in
    the queue of the one that owns it. The categories declare no `url`: they all read the one
    page, whose answers come only with its load or a tab's click.

    Args:
        name: the user name, without `@`.
    """

    def __init__(self, name: str, *, wanted: set[str] | None = None) -> None:
        self.name = name
        self.url = f"{SITE}/@{name}"
        self.wanted = wanted
        # The profile's own data (its `user` and `stats`); empty until the page is read.
        self.user: dict[str, Any] = {}
        # The items the post list held.
        self.found = 0
        self._shown = False
        self._scrolled = False
        self._posts: Any = None
        self._stories: Any = None
        self._stories_read = False
        # The (id, idx) of each item queued, so a list read again gives each once.
        self._seen: set[tuple[str, int]] = set()
        self._queues: dict[str, deque[dict[str, Any]]] = {"videos": deque(), "playlists": deque()}

    @property
    def walled(self) -> bool:
        """Whether the post list was scrolled and gave no item, on a profile with videos."""
        return self._scrolled and not self.found and dig(self.user, "stats.videoCount") != 0

    def wall(self, read: str, *,
             hint: str = "log in with another account, or try again later") -> Hidden:
        """The error of a walled post list, `read` saying what was read instead: `Hidden`, so
        another account may be tried."""
        return Hidden(f"tik: {self.name}'s post list is walled for this account; {read}",
                      hint=hint)

    async def page(self, ctx: Ctx) -> None:
        """Open the profile, when no category has yet."""
        if not self._shown:
            await self._show(ctx, posts=False)

    async def posts(self, ctx: Ctx, wanted: str) -> AsyncIterator[dict[str, Any]]:
        """Each item of `wanted` ("videos" or "playlists") as the post list scrolls.

        The first call scrolls the list to its end, queueing each item for its owner; the
        other category's call then drains its queue. Closed early (a sync caught up), it stops
        the scroll, and the other gets what was queued so far.
        """
        if self.wanted is None:
            self.wanted = _wanted_categories(getattr(ctx, "target", None))
        queue = self._queues[wanted]
        if not self._scrolled:
            self._scrolled = True
            await self._show(ctx, posts=True)
            try:
                feed = self._posts
                for read in range(REREADS + 1):
                    ended = False
                    async with feed:
                        async for data in filled(ctx, feed, page_state):
                            ended = self._take(ctx, data)
                            while queue:
                                yield queue.popleft()
                            if ended:
                                break
                    if ended or read == REREADS or not has_captcha(await ctx.document()):
                        break
                    # The list stopped short (quiet, or stalled) under a captcha that came over
                    # the grid as it scrolled (2026-11-11): watch again, wait it out (a hidden
                    # window is shown for it), and read the grid again.
                    feed = self._posts = _watch(ctx, "videos")
                    await self._clear(ctx)
            finally:
                self._posts = None
                ctx.log("info", "videos: {n} found; the profile lists {listed}", n=self.found,
                        listed=dig(self.user, "stats.videoCount"))
        while queue:
            yield queue.popleft()
        if self.wanted is not None and "playlists" not in self.wanted:
            self._queues["playlists"].clear()
        if self.wanted is not None and "videos" not in self.wanted:
            self._queues["videos"].clear()

    async def stories(self, ctx: Ctx) -> AsyncIterator[dict[str, Any]]:
        """Each story item the page's load brought; it opens the page with its watcher alone
        when no category has, and reads it once."""
        if self._stories_read:
            return
        if self._stories is None:
            await self._show(ctx, posts=False)
        feed, self._stories, self._stories_read = self._stories, None, True
        async with feed:
            async for data in feed:
                for item in items_of(data, ctx.time.wall()):
                    yield item

    async def tab(self, ctx: Ctx, category: str) -> AsyncIterator[dict[str, Any]]:
        """Each item of a clicked tab's list (`reposts`, `liked`, `favorites`), scrolled to its
        end; nothing, with a line saying so, when the page has no such tab or hides it.

        Raises:
            Changed: the tab is there but didn't take the click in `CLICK_TIMEOUT`.
        """
        await self.page(ctx)
        css = f'[data-e2e="{TABS[category]}"]'
        # A hidden tab is in the markup with `display: none`, so the page's HTML can't tell.
        if not await ctx.shown(css):
            ctx.log("info", "{tab}: this profile has no such tab", tab=category)
            return
        found = 0
        async with _watch(ctx, category) as feed:
            try:
                await ctx.click(css, timeout=CLICK_TIMEOUT)
            except NetworkError as error:
                said = str(error).partition("\n")[0]
                raise Changed(f"tik: {category}: the tab didn't open: {said}") from None
            async for data in feed:
                items = items_of(data, ctx.time.wall())
                found += len(items)
                for item in items:
                    yield item
                if has_more(data) is False:
                    break
        if not found:
            ctx.log("info", "{tab}: nothing public on this account", tab=category)

    async def links(self, ctx: Ctx) -> list[str]:
        """The ids of the posts the open page links to, in page order, each once."""
        found = [match[1] for link in (await ctx.document()).css(POST_LINKS)
                 if (match := POST_LINK.search(link.attr("href") or ""))]
        return list(dict.fromkeys(found))

    async def _show(self, ctx: Ctx, *, posts: bool) -> None:
        """Load the profile with its watchers set first, so no answer is lost, wait out any
        captcha, and read its data; a reload whose page holds none keeps what was read."""
        if posts:
            self._posts = _watch(ctx, "videos")
        if self._stories is None and not self._stories_read:
            self._stories = ctx.feed(_matches("stories"), scroll=False)
        await ctx.open(self.url)
        await ctx.pause(SETTLE, "letting the profile settle")
        await self._clear(ctx)
        self.user = page_user((await ctx.document()).script(PAGE_DATA)) or self.user
        self._shown = True

    async def _clear(self, ctx: Ctx) -> None:
        """Wait out a captcha on the profile's page (see `wait_out`)."""
        await wait_out(ctx, self.url)

    def _take(self, ctx: Ctx, data: Any) -> bool:
        """Queue one post-list page's items for their owners; whether it says it is the last."""
        more = has_more(data)
        items = [item for item in items_of(data, ctx.time.wall())
                 if (item["id"], item["idx"]) not in self._seen]
        self.found += len(items)
        for item in items:
            self._seen.add((item["id"], item["idx"]))
            cat = "playlists" if item["playlist"] else "videos"
            if self.wanted is None or cat in self.wanted:
                self._queues[cat].append(item)
        return more is False


async def wait_out(ctx: Ctx, url: str) -> None:
    """Wait out a captcha: a person solves it, else it is held as a block and `url` opened
    again, until the engine's holds run out (`Blocked`)."""
    async def gone() -> bool:
        return not has_captcha(await ctx.document())

    while has_captcha(await ctx.document()):
        if await ctx.handoff("Solve the TikTok captcha in the browser window", until=gone,
                             timeout=CAPTCHA_TIMEOUT):
            return
        await ctx.hold("TikTok captcha")
        await ctx.open(url)
        await ctx.pause(SETTLE, "letting the page settle")


class TikTok(Plugin):
    """TikTok profiles in a browser tab, and single videos from the public embed page."""

    code = "tik"
    name = "TikTok"
    domains = ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com")
    api = 2
    # Images, media and fonts load until a live check shows its pages work without them.
    hidden = True
    # Either cookie proves a login; none is needed, since public profiles are read without one.
    login = cookies("sessionid", "sid_tt", url="https://www.tiktok.com/login")
    # No known limit; TikTok's answer to heavy use is the captcha, which `Profile` holds for.
    pace = 0.2
    browser = Browser.ANY
    user_agent = TIKTOK_UA
    # No login wall is known: a logged-out visitor is shown the public profile.
    walls = ()
    unsupported = {
        f"tiktok.com/{path}": f"{why} aren't supported; give a user, a post or a search"
        for path, why in (("tag", "hashtag pages"), ("music", "sound pages"),
                          ("discover", "discover pages"), ("live", "live streams"),
                          ("explore", "the explore page"))
    }
    # Names and keys as they always were: `<date>_<post>` and `<post>` for a video, and
    # `<date>_<post>_<n>` and `<post>_<n>` for the n-th photo (from 1). A changed key downloads
    # every saved file again. `part` is the folder (videos, images, albums or the category),
    # `album` the playlist under albums.
    folder = "{part}/{album}"
    fields = ("part", "album", "idx")
    filename = "{date}_{post}_{idx}"
    key = "{post}_{idx}"

    # Before `user`, so a 16-digit id is never a user name; a link with no user is read too.
    @route(*((pattern, "post/{id}") for pattern in (
        r"tik:post/{id:\d+}", r"tik:{id:\d{16,}}", r"tiktok.com/*/video/{id:\d+}",
        r"tiktok.com/*/photo/{id:\d+}", r"tiktok.com/video/{id:\d+}",
        r"tiktok.com/photo/{id:\d+}")), single=True)
    async def post(self, ctx: Ctx, id: str) -> AsyncIterator[Post]:
        """One video or photo post, by its id: `tik:post/7301234567890123456`.

        Raises:
            NotFound: the embed page answered 404 or 410: the post is gone.
            SiteError: it refused otherwise, a 401 or 403 included, since it is public.
            Changed: it holds no post.
        """
        for post in await embed(ctx, id.removeprefix("post/")):
            yield post

    # Followed in the browser, the way `fbk`'s `watch` follows fb.watch.
    @route(*((pattern, "short/{code}") for pattern in (
        r"vm.tiktok.com/{code:[\w-]+}", r"vt.tiktok.com/{code:[\w-]+}",
        r"tiktok.com/t/{code:[\w-]+}")), single=True)
    async def short(self, ctx: Ctx, code: str) -> AsyncIterator[Post]:
        """One post by its short link (`vm.`, `vt.` or `tiktok.com/t/`), followed in the
        browser and read from the video it lands on.

        Raises:
            NotFound: the link didn't land on a video or photo post.
        """
        link = f"https://vm.tiktok.com/{code.removeprefix('short/')}"
        landed = (await ctx.open(link)).url
        found = POST_LINK.search(urlsplit(landed).path)
        if found is None:
            raise NotFound(f"tik: {link} didn't lead to a post",
                           hint="check the link in a browser; only video and photo links work")
        for post in await embed(ctx, found[1]):
            yield post

    @route("tik?{query}", r"tiktok.com/search?q={query:.+}", newest_first=False)
    async def search(self, ctx: Ctx, query: str) -> AsyncIterator[Target]:
        """A search's posts, such as `tik?cats`, each as `tik:post/<id>`: the Videos tab
        scrolled in the browser, its `api/search/item/full/` answers read.

        Raises:
            Blocked: a captcha nobody solved, held until the engine's holds run out.
        """
        url = f"{SITE}/search/video?q={quote_plus(query)}"
        seen: set[str] = set()
        feed = ctx.feed(SEARCH_API, scroll=True, cursor=search_state, resume=True)
        await ctx.open(url)
        await ctx.pause(SETTLE, "letting the search settle")
        await wait_out(ctx, url)
        async with feed:
            async for data in filled(ctx, feed, search_state):
                for found in dig(data, "item_list || itemList") or []:
                    post_id = str(dig(found, "id") or "")
                    if post_id and post_id not in seen:
                        seen.add(post_id)
                        yield Target(f"tik:post/{post_id}")
                if not (search_state(data) or (False,))[0]:
                    break

    @route("tik:{me:me}", shown="tik:me", discovers=True)
    async def me(self, ctx: Ctx, me: str) -> AsyncIterator[Target]:
        """Every account the login follows, each as `tik:<uniqueId>`: `tik:me`. The own
        profile's Following list is opened in the logged-in browser, its answers read.

        Raises:
            LoginError: no login, so no account to read.
            Changed: the profile has no Following list to open.
        """
        if not any(ctx.cookies.get(name) for name in ("sessionid", "sid_tt")):
            raise LoginError("tik: not logged in, so there is no account to read",
                             hint="log in: flnd auth login tik")
        async with ctx.feed(FOLLOWING_API, scroll=True, cursor=search_state, resume=True) as feed:
            await ctx.open(OWN_PAGE)
            await ctx.pause(SETTLE, "letting the profile settle")
            await wait_out(ctx, OWN_PAGE)
            try:
                await ctx.click(FOLLOWING_TAB, timeout=CLICK_TIMEOUT)
            except NetworkError:
                raise Changed("tik:me: the profile's Following list didn't open") from None
            async for data in filled(ctx, feed, search_state):
                for entry in dig(data, "userList || user_list") or []:
                    name = dig(entry, "user.uniqueId || uniqueId")
                    if name:
                        yield Target(f"tik:{name}")
                if not (search_state(data) or (False,))[0]:
                    break

    # The `@` is in the link's pattern, so `tiktok.com/@@example_creator` is refused.
    @route("tik:{user}", r"tiktok.com/@{user:[\w.]+}", newest_first=False)
    async def user(self, ctx: Ctx, user: str) -> Profile:
        """A profile, by its user name: `tik:example_creator`, `tik:@example_creator` or
        `https://www.tiktok.com/@example_creator`. Nothing is opened yet."""
        return Profile(user, wanted=_wanted_categories(getattr(ctx, "target", None)))

    # Newest first, apart from pinned videos at the top, which the engine's early stop allows for.
    @user.category(newest_first=True)
    async def videos(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """The videos and photo posts in no playlist.

        Raises:
            Hidden: the post list was walled (it gave no item, 2026-11-11): the posts
                the page links to were read from their embed pages first. It stands for
                so another account may be tried.
        """
        async for item in profile.posts(ctx, "videos"):
            yield post_of(ctx, item, "videos")
        if not profile.walled:
            return
        links = await profile.links(ctx)
        for post_id in links:
            try:
                posts = await embed(ctx, post_id)
            except Blocked:
                raise
            except (SiteError, NetworkError) as error:
                ctx.log("warn", "{id}: {error}", id=post_id, error=str(error))
                continue
            for post in posts:
                yield post
        raise profile.wall(f"the {len(links)} posts its page links to were read instead")

    @user.category
    async def stories(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """The stories the profile's page loads with."""
        async for item in profile.stories(ctx):
            yield post_of(ctx, item, "stories")

    @user.category(newest_first=True)
    async def playlists(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """The videos in a playlist, each under `albums/<playlist title>/`.

        Raises:
            Hidden: the post list was walled, as for `videos`, whose fallback reads the
                posts the page links to, playlists' included.
        """
        async for item in profile.posts(ctx, "playlists"):
            yield post_of(ctx, item, "videos")
        if profile.walled:
            raise profile.wall("its playlists can't be told from its other posts",
                               hint="log in with another account, or download its videos "
                                    "category too: it reads the posts the page links to, "
                                    "playlists' included")

    # Ordered by when each was reposted, not posted, so not newest first.
    @user.category(own=False)
    async def reposts(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """The Reposts tab."""
        async for item in profile.tab(ctx, "reposts"):
            yield post_of(ctx, item, "reposts")

    @user.category(own=False)
    async def liked(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """The Liked tab, when the account made it public."""
        async for item in profile.tab(ctx, "liked"):
            yield post_of(ctx, item, "liked")

    @user.category(own=False)
    async def favorites(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """The Favorites tab, when the account made it public."""
        async for item in profile.tab(ctx, "favorites"):
            yield post_of(ctx, item, "favorites")

    # Keyed as it always was, by its path, whatever the content's extension.
    @user.category(folder="profile", key="profile/avatar_{post}.jpg")
    async def profile(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """The largest profile picture (`avatarLarger`), as profile/avatar_<id>.jpg."""
        await profile.page(ctx)
        url = dig(profile.user, "user.avatarLarger")
        if url:
            # The path's last part is "<id>~<size options>.jpeg"; the id names the picture.
            picture = PurePosixPath(urlsplit(url).path).name.split("~")[0]
            yield Post(picture, [File(url, name=f"avatar_{picture}.jpg")],
                       headers=cdn_headers(ctx.cookies), part="profile", album="", idx="")

    async def refresh(self, ctx: Ctx, post: Post) -> Post:
        """The post's file again, from one fetch of its embed page.

        Raises:
            NotFound: the post is gone, or no longer has this file; or it is the profile
                picture, which has no embed page.
        """
        if post.fields.get("part") == "profile":
            return await super().refresh(ctx, post)
        idx = post.fields.get("idx", "")
        fresh = next((found for found in await embed(ctx, post.id)
                      if found.fields["idx"] == idx), None)
        if fresh is None:
            raise NotFound(f"tik: post {post.id} no longer has its file {idx or 1}")
        return post.replace(files=fresh.files, headers=fresh.headers)


# ---------------------------------------------------------------------------------------------
# Reading TikTok's answers
# ---------------------------------------------------------------------------------------------

def _matches(category: str) -> Callable[[str], bool]:
    """A feed match for the answers of `category`'s list: one of its API paths in the URL."""
    return lambda url: any(api in url for api in FEEDS[category])


def _watch(ctx: Ctx, category: str) -> Any:
    """A scrolling feed of `category`'s list, ended by its own `hasMore: false`."""
    return ctx.feed(_matches(category), scroll=True, cursor=page_state, resume=True)


async def filled(ctx: Ctx, feed: AsyncIterator[Any],
                 state: Callable[[Any], Any]) -> AsyncIterator[Any]:
    """A scrolled feed's answers, but for an empty one that still says it has more.

    That answer holds, once in a row, and the scroll goes on; a second one ends the list.
    """
    came = retried = False
    async for data in feed:
        more = (state(data) or (False,))[0]
        if more and not any(isinstance(v, list) and v for v in data.values()):
            if retried:
                ctx.log("warn", "an empty page still says hasMore; the list ends here")
                return
            retried = True
            await ctx.hold("TikTok sent an empty page", reset=came)
            came = False
            continue
        retried, came = False, True
        yield data


async def embed(ctx: Ctx, post_id: str) -> list[Post]:
    """The posts of one video or photo post, from its public embed page.

    Raises:
        NotFound: the page answered 404 or 410.
        SiteError: it refused otherwise, a 401 or 403 included.
        Changed: it holds no `itemStruct`.
    """
    # Checked here: the embed page is public, so a 401 or 403 there is no login wall.
    response = await ctx.fetch(Request(f"{SITE}/embed/v2/{post_id}", check=False))
    if response.status in (404, 410):
        raise NotFound(f"tik: post {post_id} is gone (HTTP {response.status})",
                       status=response.status)
    if response.status >= 400:
        raise SiteError(f"tik: the embed page of {post_id} answered HTTP {response.status}",
                        status=response.status)
    item = dig(response.html.script("__NEXT_DATA__"), "props.pageProps.itemStruct")
    if not item:
        raise Changed(f"tik: the embed page of {post_id} holds no post")
    return [post_of(ctx, found, "videos")
            for found in items_of({"itemList": [item]}, ctx.time.wall())]


def post_of(ctx: Ctx, item: dict[str, Any], category: str) -> Post:
    """One file of an item (a video, or one photo of a photo post) as a post of its own.

    A photo's key counts from 1 even in a one-photo post, which no engine field gives, so
    each photo is a post with its `idx`. A photo's name is pinned to today's
    `<date>_<post>_<n>.jpg`, whatever the CDN's URL ends with; a video's extension is the
    content's. A pinned post sits out of date order, and says so.

    Args:
        ctx: the target's context, whose cookies go to the CDN.
        item: one of `items_of`'s items.
        category: the category it is listed for; a `videos` item goes under videos, images or
            albums by its kind and playlist.
    """
    part, album = category, ""
    if category == "videos" and item["playlist"]:
        part, album = "albums", item["playlist"]
    elif category == "videos":
        part = "videos" if item["kind"] == "video" else "images"
    idx = str(item["idx"] or "")
    name = None
    if item["kind"] == "image":
        day = item["date"] and datetime.datetime.fromtimestamp(item["date"], datetime.UTC)
        name = "_".join(piece for piece in (day and f"{day:%Y-%m-%d}", item["id"], idx)
                        if piece) + ".jpg"
    urls = item["urls"]
    file = File(urls[0], name=name) if len(urls) == 1 else File(mirrors=urls, name=name)
    return Post(item["id"], [file], date=item["date"], pinned=item["pinned"],
                headers=cdn_headers(ctx.cookies), owner=item.get("owner"), category=category,
                part=part, album=album, idx=idx)


def cdn_headers(cookies: Mapping[str, str]) -> dict[str, str]:
    """The headers that keep the CDN from a 403: Referer, user agent and the session cookies,
    each cookie only when it has a value (an empty `sessionid=` is no session)."""
    headers = {"Referer": f"{SITE}/", "User-Agent": TIKTOK_UA}
    sent = [f"{name}={cookies[name]}" for name in CDN_COOKIES if cookies.get(name)]
    if sent:
        headers["Cookie"] = "; ".join(sent)
    return headers


def has_captcha(document: Document) -> bool:
    """Whether a page shows a captcha: its container, or its words in the text a reader sees."""
    return (document.first(CAPTCHA_CSS) is not None
            or bool(CAPTCHA_TEXT.search(document.visible)))


def has_more(data: Any) -> bool | None:
    """What one list answer says of a next page: True, False, or None when it says nothing."""
    if not isinstance(data, dict) or "hasMore" not in data:
        return None
    return bool(data.get("hasMore"))


def page_state(data: Any) -> tuple[bool, str | None] | None:
    """`(more, cursor)` of one list answer, for `ctx.feed(cursor=…)`; None for a body that
    isn't a list's page (an error's, a captcha's)."""
    more = has_more(data)
    if more is None:
        return None
    cursor = data.get("cursor")
    return more, str(cursor) if cursor not in (None, "") else None


def search_state(data: Any) -> tuple[bool, str | None] | None:
    """`(more, cursor)` of a search or follow list answer (`has_more` or `hasMore`), or None."""
    if not isinstance(data, dict) or not {"has_more", "hasMore"} & data.keys():
        return None
    cursor = data.get("cursor")
    return bool(data.get("has_more", data.get("hasMore"))), (
        str(cursor) if cursor not in (None, "") else None)


def page_user(data: Any) -> dict[str, Any]:
    """The profile's `userInfo` (its `user` and `stats`) from the page's data script, or {}."""
    for key in USER_KEYS:
        data = data.get(key) if isinstance(data, dict) else None
    return data if isinstance(data, dict) else {}


def items_of(data: Any, now: float) -> list[dict[str, Any]]:
    """The media items of one list answer (`itemList`, `storyList` or `story_list`).

    Args:
        data: an answer's JSON.
        now: the run's Unix time, the latest a post's date may be.

    Returns:
        Dicts with `id`, `date` (Unix seconds, or None), `playlist` (its name or None),
        `pinned` (whether it sits pinned at the top of the profile), `owner` (its author's
        `uniqueId`, or None), `urls`
        (the file's CDN addresses, best first), `kind` ("video" or "image") and `idx`: 0 for a
        video, else the photo's place from 1. A photo post has `imagePost`, and its video, if
        any, is never read.
    """
    if not isinstance(data, dict):
        return []
    items = []
    for raw in dig(data, "itemList || storyList || story_list") or []:
        if not isinstance(raw, dict):
            continue
        item_id = str(raw.get("id") or raw.get("aweme_id") or "")
        if not item_id:
            continue
        common = {"id": item_id, "date": stamp_of(raw, item_id, now),
                  "playlist": dig(raw, "playlist.name || playlist_info.name"),
                  # A post pinned to the profile's top; the mark is unconfirmed live.
                  "pinned": bool(raw.get("isPinnedItem")),
                  "owner": dig(raw, "author.uniqueId")}
        images = dig(raw, "imagePost.images")
        if images:
            for idx, image in enumerate(images, 1):
                path = "imageURL.urlList || displayImage.urlList || thumbnail.urlList"
                urls = mirrors(dig(image, path) or [])
                if urls:
                    items.append({**common, "urls": urls, "kind": "image", "idx": idx})
        elif urls := best_video(raw.get("video") or {}):
            items.append({**common, "urls": urls, "kind": "video", "idx": 0})
    return items


def stamp_of(raw: dict[str, Any], item_id: str, now: float) -> int | None:
    """A post's Unix time: `createTime`, else the id's upper 32 bits (a Snowflake), else None.

    The engine turns it into a UTC day, so a video is named alike on every machine.
    """
    created = _unix(raw.get("createTime") or raw.get("create_time"), now)
    if created is not None:
        return created
    return _unix(int(item_id) >> 32, now) if item_id.isdigit() else None


def _unix(value: Any, now: float) -> int | None:
    """`value` as Unix seconds when it is a whole number (or its digits) from 2004 to `now`."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int):
        return None
    return value if EARLIEST <= value <= now + SKEW else None


def mirrors(urls: Sequence[Any]) -> tuple[str, ...]:
    """Every CDN address of a file, in order, empty ones and repeats dropped.

    TikTok refuses some posts on one CDN host and not another (2026-11-13), so every address
    is kept and the engine tries the next one when one refuses.
    """
    return tuple(dict.fromkeys(str(url) for url in urls if url))


def best_video(video: dict[str, Any]) -> tuple[str, ...] | None:
    """The addresses of a video's best whole MP4 (`best` of `bitrateInfo`), else `playAddr`.

    DASH entries are left out, since one saved as an MP4 has no sound; `downloadAddr`, the
    watermarked copy, is never taken.
    """
    streams, addresses = [], {}
    for entry in video.get("bitrateInfo") or []:
        if not isinstance(entry, dict) or _is_dash(entry):
            continue
        urls = mirrors(dig(entry, "PlayAddr.UrlList") or [])
        if not urls:
            continue
        size = entry.get("PlayAddr") or {}
        streams.append(Stream(urls[0], height=_whole(size.get("Height")),
                              width=_whole(size.get("Width")),
                              bitrate=_whole(entry.get("Bitrate"))))
        addresses.setdefault(urls[0], urls)
    chosen = best(streams)
    if chosen is not None:
        return addresses[chosen.url]
    address = video.get("playAddr")
    if isinstance(address, dict):
        address = address.get("urlList") or []
    return mirrors([address] if isinstance(address, str) else address or []) or None


def _whole(value: Any) -> int:
    """A size or bitrate as TikTok sends it, as a whole number; 0 for a string or a null."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _is_dash(entry: dict[str, Any]) -> bool:
    """Whether a `bitrateInfo` entry is a DASH stream (video only, or a manifest)."""
    play = entry.get("PlayAddr") or {}
    marks = (entry.get("Format"), entry.get("GearName"), play.get("UrlKey"))
    urls = play.get("UrlList") or []
    return (any("dash" in str(mark or "").lower() for mark in marks)
            or any(urlsplit(str(url)).path.lower().endswith(".mpd") for url in urls))
