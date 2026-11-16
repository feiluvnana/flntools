"""TikTok: profile videos, photo posts, stories, playlists, reposts, liked, and favorites.

What it holds: the `TikTok` source, the `Profile` it shares between a profile's categories,
and the readers of TikTok's JSON (`items_of`, `best_video`, `stamp_of`, `page_state`).

Who calls it: the engine, through the source API (`flnd.source`): `TikTok.user` and its
categories for a profile, `TikTok.video` for one post. It calls only `flnd.source` and the stdlib.

How a profile is read (`docs/sources/tik` has the whole story):
  1. The `user` route builds a `Profile` and opens nothing yet.
  2. The first category that needs the page opens `https://www.tiktok.com/@<user>` in the
     browser, with response watchers (`site.feed`) set up first so no response is lost, waits for
     it to settle, and waits out any captcha (a person solves it, or flnd backs off and reloads).
  3. `videos` and `playlists` share one scroll of the post list (`/api/post/item_list`): each
     item goes to the queue of the category that owns it, and the scroll ends when a response
     says `hasMore: false`.
  4. `stories` come with the page load; `reposts`, `liked` and `favorites` click their tab and
     scroll its own list to its end.
  5. `profile` reads the profile picture from the page's server-rendered data.

One video or photo post, by id or link, is read from the public embed page without a browser.

Routes and categories:
- `video`: `tik:<id of 16 digits or more>`, `tiktok.com/[@<user>/]video/<id>` and
  `.../photo/<id>`. It is declared before `user`, so a long number is never a user name.
- `user`: `tik:<user>`, `tik:@<user>`, `tiktok.com/@<user>`, with the categories `videos`,
  `stories`, `playlists`, `reposts`, `liked`, `favorites` and `profile`, in that order.
- A short link (`vm.tiktok.com/...`) matches no route: the engine follows its redirect once.

How a list ends: TikTok's own `hasMore: false` in a list response (`page_state`), or the bottom
of the page staying quiet (the feed's idle end). Nothing else ends a list here; the engine's
early stops close the generator from outside.

Login: `cookies("sessionid", "sid_tt")`, read from a browser or a cookie file. It is never
required: a public profile is read logged out, with a warning. The CDN is sent `tt_chain_token`
and `sessionid` (`cdn_headers`), each only when it has a value.

Site quirks, with the date seen:
- A slider or puzzle captcha shows a few seconds after the page looks loaded (2026-11-11), and
  sometimes over the grid mid-scroll; a person gets `CAPTCHA_TIMEOUT` to solve it, else it is
  backed off as a rate limit and the page reloaded (2026-11-12).
- A walled post list gives no item at all; the posts linked on the page are then read from their
  embed pages instead (2026-11-11).
- `createTime` may be missing or a placeholder; the id's upper 32 bits hold the Unix time
  (2026-11-11).
- The CDN answers 403 without the Referer, the user agent and the session cookies (2026-11-11),
  and refuses some posts on one CDN host but not another, so every address is a mirror
  (2026-11-13).
- `bitrateInfo` may hold DASH entries, video only, which are skipped (2026-11, unconfirmed live).
- The Liked tab is served from `/api/favorite/item_list` and Favorites from
  `/api/user/collect/item_list`: the names are swapped (2026-11-11).
- Pinned videos sit at the top of the post list, out of date order: the engine's early stops
  wait for a streak, so they don't end the list (date not recorded).

Folder layout under `.flnd/media/tik/<username>/`:
  videos/, images/, stories/, reposts/, ...   <date>_<post>[_<n>].<ext>
  albums/<playlist name>/                     <date>_<post>[_<n>].<ext>
  profile/                                    avatar_<id>.jpg

Rules a change must keep:
  - File names and history keys are what they always were: `<date>_<post>` and `<post>` for a
    video, `<date>_<post>_<n>` and `<post>_<n>` for the n-th photo (n from 1). A changed key makes
    every saved file download again.
  - Each photo of a photo post is a `Post` of its own (same id, one file, its `idx` field),
    because a key's `_<n>` counts from 1 even for a one-photo post, which no engine field gives.
  - The engine owns the early stops (`--when`, a sync that caught up). The source yields items in
    the listing's order and stops only where TikTok says the list ends (`hasMore: false`).
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from pathlib import PurePosixPath
from typing import (
    TYPE_CHECKING,
    Any,
    NamedTuple,
)
from urllib.parse import urlsplit

from flnd.source import (
    Blocked,
    File,
    LoginError,
    NetworkError,
    Post,
    Site,
    SiteError,
    Source,
    cookies,
    dig,
    target,
)

if TYPE_CHECKING:
    import flnd.source

# The user agent TikTok's embed page and CDN have always been sent: a desktop Chrome.
TIKTOK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/128.0.0.0 Safari/537.36")

# How a captcha is found: how many back-offs it gets before the target fails, its containers
# (TikTok has used each of these ids and classes, seen 2026-11-11), and the words it shows.
CAPTCHA_ATTEMPTS = 3
CAPTCHA_CSS = ('#captcha-verify-container-main-page, #captcha_container, [id*="captcha-verify"], '
               '[class*="captcha_verify"], [class*="captcha-verify"]')
CAPTCHA_TEXT = re.compile(
    r"Drag the slider|Drag the puzzle|Verify to continue|Select 2 objects", re.IGNORECASE,
)
# The links to a profile's posts, read from the page when its API is walled.
POST_LINKS = 'a[href*="/video/"], a[href*="/photo/"]'
# Seconds to let the profile settle before looking for a captcha: it shows a few seconds after
# the load event. The value is a margin over that; where it was measured isn't recorded.
SETTLE = 5.0
# Seconds a tab (Reposts, Liked, Favorites) gets to appear and take a click before it is skipped.
CLICK_TIMEOUT = 10.0
# Seconds a person gets to solve a captcha before the source backs off instead.
CAPTCHA_TIMEOUT = 60.0
# The profile's data, server-rendered into the page: the script's id, and the keys to the user
# under it. "webapp.user-detail" is one key with a dot in it, so a `dig` path can't name it.
PAGE_DATA = "__UNIVERSAL_DATA_FOR_REHYDRATION__"
USER_KEYS = ("__DEFAULT_SCOPE__", "webapp.user-detail", "userInfo")
SITE = "https://www.tiktok.com"
# The earliest Unix time taken as a post's date: 2004-01-01 UTC, the engine's first year. A
# smaller `createTime` (0, a small number) is a placeholder, and the id is read instead.
EARLIEST = 1_072_915_200
# The CDN cookies, in the order they are sent: the chain token TikTok sets for every visitor,
# and the login's session.
CDN_COOKIES = ("tt_chain_token", "sessionid")


class Feed(NamedTuple):
    """One list the profile loads: its category, and the API path its pages come from."""

    # The category the list fills: "videos", "stories", "reposts", "favorites" or "liked".
    category: str
    # The API path its pages come from, without the host; `is_feed_of` matches responses by it.
    api: str


# The API paths whose responses fill each category; a category can have more than one (stories).
FEEDS = (
    Feed("videos", "/api/post/item_list"),
    Feed("stories", "/api/story/item_list"),
    Feed("stories", "/api/user/story"),
    Feed("reposts", "/api/repost/item_list"),
    Feed("favorites", "/api/user/collect/item_list"),
    Feed("liked", "/api/favorite/item_list"),
)


def is_feed_of(category: str) -> Callable[[str], bool]:
    """A match for the responses of the feeds listing `category`, as `site.feed` takes it.

    Args:
        category: a feed's category: "videos", "stories", "reposts", "favorites" or "liked".

    Returns:
        A function of a response's URL: whether one of the category's API paths is in it.
    """
    apis = [feed.api for feed in FEEDS if feed.category == category]
    return lambda url: any(api in url for api in apis)


def has_captcha(site: Site) -> bool:
    """Whether the page shows a captcha: its container, or its words in the text a reader sees.

    Args:
        site: the target's site, whose open page is checked.

    Returns:
        True when a captcha container is on the page or its words are visible.
    """
    page = site.document()
    return page.first(CAPTCHA_CSS) is not None or bool(CAPTCHA_TEXT.search(page.visible))


def has_more(data: Any) -> bool | None:
    """What one list response says of a next page: True, False, or None when it says nothing.

    Args:
        data: a response's JSON.

    Returns:
        True or False as the response's `hasMore` says, or None when it has none.
    """
    if not isinstance(data, dict) or "hasMore" not in data:
        return None
    return bool(data.get("hasMore"))


class Profile:
    """Profile shared state: data, lists, page opened on demand.

        profile = Profile(site, "example_creator")
        for item in profile.feed("videos"): ...   # post list, direct or page

    The route builds it and asks nothing yet. The response watchers must exist before the page
    loads, or the first responses are lost, so the first category that needs the page opens it
    with the watchers it needs; a later category that needs the post list after the page was
    opened without it reloads the page with its watcher.

    Args:
        site: the target's site, whose tab is the profile's.
        name: the user name, without `@`.
    """

    def __init__(self, site: Site, name: str):
        """Set profile URL and initial empty state."""
        self.site = site
        self.url = f"{SITE}/@{name}"
        # The profile's own data (its `user` with `secUid` and `id`, its `stats`); empty until read.
        self.user: dict[str, Any] = {}
        # How many items each feed gave, saved or not, so a second run still sees a list grow.
        self.found = {feed.category: 0 for feed in FEEDS}
        self._loaded: bool | None = None
        # The post list's pages from the browser, scrolling to the end;
        # and the stories' page responses (no scrolling: they come with the load).
        self._posts: flnd.source.Feed | None = None
        self._stories: flnd.source.Feed | None = None
        self._started = False
        # The post feed's items waiting for the category that wants them: a playlist's or not.
        self._queues: dict[str, deque[dict[str, Any]]] = {"videos": deque(), "playlists": deque()}
        self._told = False

    @property
    def on_page(self) -> bool:
        """Whether the profile's page was opened in the browser."""
        return bool(self._loaded)

    def open(self, *, feeds: bool) -> bool:
        """Open profile on first call, or reload with watchers if `feeds` is needed later.

        Args:
            feeds: whether the post list and stories watchers are needed.

        Returns:
            Whether the profile is open.
        """
        if self._loaded is None:
            if feeds:
                self._posts = self.site.feed(is_feed_of("videos"), scroll=True, cursor=page_state)
                self._stories = self.site.feed(is_feed_of("stories"))
            self._loaded = self._load()
            return self._loaded
        if feeds and self._posts is None and not self._started:
            return self._watch(posts=True, stories=self._stories is None)
        return self._loaded

    def feed(self, wanted: str) -> Iterator[dict[str, Any]]:
        """Stream items of `wanted` as the post feed scrolls, or drain queued ones.

        The first call scrolls the post list, handing each item to the queue of the category
        that owns it; a later call (the other of the two categories) only drains its queue.

        Args:
            wanted: "videos" (items in no playlist) or "playlists".

        Yields:
            Each item of `wanted`, as `items_of` gives it, in the list's order.
        """
        queue = self._queues[wanted]
        if not self._started:
            yield from self._read_posts(wanted)
        while queue:
            yield queue.popleft()

    def stories(self) -> Iterator[dict[str, Any]]:
        """The profile's stories from the page's watched responses.

        The stories come with the page load, so the profile is opened with its watchers when it
        wasn't yet. The watcher is read once and closed: a second call yields nothing.

        Yields:
            Each story item, as `items_of` gives it.
        """
        if self._stories is None and not self.open(feeds=True):
            return
        stories, self._stories = self._stories, None
        batches = list(stories or ())
        if stories is not None:
            stories.close()
        for data in batches:
            items = items_of(data)
            self.found["stories"] += len(items)
            yield from items

    def tab(self, category: str, tab: str) -> Iterator[dict[str, Any]]:
        """Open one profile tab in the browser by a click and read its feed to the end.

        Args:
            category: the category the tab fills, for its feed and its messages.
            tab: the tab's `data-e2e` name, such as "repost-tab".

        Yields:
            Each item of the tab's list, as `items_of` gives it; nothing when the account shows
            no such tab.
        """
        if not self.open(feeds=False):
            return
        css = f'[data-e2e="{tab}"]'
        if not self.site.document().css(css):
            self.site.info("{tab}: not public on this account", tab=category)
            return
        for data in self._click_batches(category, css):
            items = items_of(data)
            self.found[category] += len(items)
            yield from items
        if not self.found[category]:
            self.site.info("{tab}: nothing public on this account", tab=category)

    def wait_out_captcha(self) -> None:
        """Until no captcha is left: solved by user or backed off and reloaded.

        Raises:
            Blocked: the captcha was still there after `CAPTCHA_ATTEMPTS` back-offs.
        """
        attempts = 0
        while has_captcha(self.site):
            if self.site.handoff(until=lambda: not has_captcha(self.site), timeout=CAPTCHA_TIMEOUT,
                                 message="Solve the TikTok captcha in the browser window"):
                return
            if attempts >= CAPTCHA_ATTEMPTS:
                raise Blocked("TikTok captcha wasn't solved",
                              hint="solve it in a browser, then try again")
            attempts += 1
            # Unsolved captcha handled as rate limit: back off, then reload page.
            self.site.back_off("TikTok captcha")
            self.site.open(self.url)
            self.site.pause(SETTLE, "letting the profile settle")

    def _click_batches(self, category: str, css: str) -> Iterator[Any]:
        """Click a profile tab and yield its responses live while scrolling to the end.

        The watcher is set up before the click, so the tab's first page isn't missed. A tab
        that can't be clicked in `CLICK_TIMEOUT` is skipped with a warning.

        Args:
            category: the category the tab fills: its feed's API paths and its messages.
            css: the selector of the tab to click.

        Yields:
            Each list response's JSON, until one says `hasMore: false` or the feed ends.
        """
        with self.site.feed(is_feed_of(category), scroll=True, cursor=page_state) as responses:
            try:
                self.site.click(css, timeout=CLICK_TIMEOUT)
            # Nothing to click in time, or the tab closed: that tab is skipped.
            except NetworkError as error:
                self.site.warn("{tab}: the tab didn't open: {error}", tab=category,
                               error=str(error).splitlines()[0])
                return
            for data in responses:
                yield data
                if has_more(data) is False:
                    return

    def _watch(self, *, posts: bool = False, stories: bool = False) -> bool:
        """Start feed watchers and load the profile page (again).

        Args:
            posts: whether to watch the post list (`/api/post/item_list`), scrolling.
            stories: whether to watch the story lists, which come with the load.

        Returns:
            Whether the profile is open; `_load` raises rather than return False.
        """
        if posts:
            self._posts = self.site.feed(is_feed_of("videos"), scroll=True, cursor=page_state)
        if stories:
            self._stories = self.site.feed(is_feed_of("stories"))
        self._loaded = self._load()
        return self._loaded

    def _load(self) -> bool:
        """Load the profile, wait out any captcha, and read its own data.

        Returns:
            True once the page is open and any captcha gone.

        Raises:
            SiteError: the profile page didn't load.
            Blocked: a captcha stayed (`wait_out_captcha`).
        """
        if not self.site.open(self.url):
            raise SiteError("the profile didn't load")
        # The captcha shows a few seconds after the load event, over a page that looks ready.
        self.site.pause(SETTLE, "letting the profile settle")
        self.wait_out_captcha()
        # Reload finding no data keeps earlier data, preserving secUid.
        self.user = page_user(self.site.document().script(PAGE_DATA)) or self.user or {}
        return True

    def _read_posts(self, wanted: str) -> Iterator[dict[str, Any]]:
        """Scroll the post feed to its end, yielding `wanted` items live.

        The engine may close this generator early (`--when`, or a sync that caught up); the
        `finally` then stops the watcher, and `playlists` gets only what was queued so far.

        A captcha may come over the grid while it scrolls (2026-11-11): when the feed then gave
        nothing at all, the watcher is set up again, the captcha waited out (which reloads the
        page) and the grid read again.

        Args:
            wanted: the category reading: "videos" or "playlists"; the other one's items wait
                in its queue.

        Yields:
            Each item of `wanted`, as the list scrolls.
        """
        self._started = True
        if not self.open(feeds=True):
            return
        try:
            while self._posts is not None:
                ended = yield from self._drain_posts(wanted)
                if ended or self.found["videos"] or not has_captcha(self.site):
                    break
                # A captcha came over the grid as it scrolled, and the feed gave nothing: watch
                # again, wait the captcha out (it reloads the page), and read the grid again.
                self._posts = self.site.feed(is_feed_of("videos"), scroll=True, cursor=page_state)
                self.wait_out_captcha()
        finally:
            self._end()

    def _drain_posts(self, wanted: str) -> Generator[dict[str, Any], None, bool]:
        """Yield `wanted` items batch by batch; return True once a response says it is the end.

        Args:
            wanted: "videos" or "playlists"; every item is queued for its owner first.

        Yields:
            Each item of `wanted`, after each response.

        Returns:
            True when a response said `hasMore: false`; False when the feed ran out by itself
            (a quiet bottom, or a closed feed).
        """
        assert self._posts is not None
        queue = self._queues[wanted]
        for data in self._posts:
            items = items_of(data)
            self.found["videos"] += len(items)
            for item in items:
                self._queues["playlists" if item["playlist"] else "videos"].append(item)
            while queue:
                yield queue.popleft()
            if has_more(data) is False:
                return True
        return False

    def _end(self) -> None:
        """Stop watching post feed, reporting total videos found."""
        if self._posts is not None:
            self._posts.close()
            self._posts = None
        if not self._told:
            self._told = True
            self.site.info(
                "videos: {n} found; the profile lists {listed}",
                n=self.found["videos"], listed=dig(self.user, "stats.videoCount"),
            )


class TikTok(Source):
    """TikTok profiles in a browser, and single videos from the embed page."""

    code = "tik"
    name = "TikTok"
    # Short-link hosts (vm./vt.tiktok.com) are subdomains: short link is read by
    # the engine, which follows it once when no route matches.
    domains = ("tiktok.com",)
    api = 1
    # Either cookie proves a login; none is needed, since public profiles are read without one.
    login = cookies("sessionid", "sid_tt")
    user_agent = TIKTOK_UA
    # No known limit; a captcha doubles the gap.
    pace = 0.2
    # What a post's files are named and keyed by, as they always were: `<date>_<post>` for a video
    # and `<date>_<post>_<n>` for the n-th photo (n from 1), the key the same without the date.
    # `idx` is empty for a video, so the `_` before it goes. A post's `part` is its folder (videos,
    # images, a category, or albums), and its `album` the playlist, under albums.
    folder = "{part}/{album}"
    fields = ("part", "album", "idx")
    filename = "{date}_{post}_{idx}"
    key = "{post}_{idx}"

    # `video` comes before `user`, so a 16-digit id is never a user name. A video or photo URL with
    # no user before it is read too, as the old reader did.
    @target(r"tik:{id:\d{16,}}", r"tiktok.com/*/video/{id:\d+}", r"tiktok.com/*/photo/{id:\d+}",
            r"tiktok.com/video/{id:\d+}", r"tiktok.com/photo/{id:\d+}")
    def video(self, site: Site, id: str) -> Iterator[Post]:
        """One video or photo post, by its id: `tik:7301234567890123456`.

        Args:
            site: the target's site; the embed page needs no browser.
            id: the post's numeric id, from the target or the URL.

        Yields:
            A post per video, or one per photo of a photo post; nothing when the embed page
            failed (a warning says why).
        """
        yield from embed_posts(site, id)

    # Link's `@` is in pattern, so regex is explicit; default `{user}` would
    # drop a second `@`, and `tiktok.com/@@name` is refused as it always was.
    @target("tik:{user}", r"tiktok.com/@{user:[\w.]+}")
    def user(self, site: Site, user: str) -> Profile:
        """A profile, by its user name; a `@` in front of it is dropped. Nothing is opened yet.

        Args:
            site: the target's site, whose tab the profile is opened in.
            user: the user name, without `@`.

        Returns:
            The `Profile` every category of the target shares.
        """
        return Profile(site, user)

    # Newest first, apart from pinned videos at the top, which the engine's early stop allows for.
    @user.category(newest_first=True)
    def videos(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The videos and photo posts that are in no playlist.

        When the post list gave nothing at all on a page that did open (a walled API,
        2026-11-11), each post the page links to is read from its embed page instead.

        Args:
            site: the target's site, which the posts are built for.
            profile: the `Profile` the route built, shared with the other categories.

        Yields:
            A post per file, as `post_of` builds it.
        """
        for item in profile.feed("videos"):
            yield post_of(site, item, "videos")
        if not profile.found["videos"] and profile.on_page:
            # A walled API gave nothing: read each post linked on the page from its embed page.
            links = [match.group(2) for link in site.document().css(POST_LINKS)
                     if (match := re.search(r"/(video|photo)/(\d+)", link.attr("href") or ""))]
            for video_id in dict.fromkeys(links):
                yield from embed_posts(site, video_id)

    @user.category
    def stories(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile's stories: their list API, or the page they come with.

        Args:
            site: the target's site, which the stories' posts are built for.
            profile: the `Profile` the route built, shared with the other categories.

        Yields:
            A post per file, as `post_of` builds it.
        """
        for item in profile.stories():
            yield post_of(site, item, "stories")

    @user.category(newest_first=True)
    def playlists(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The videos that are in a playlist, each under `albums/<playlist title>/`.

        Args:
            site: the target's site, which the posts are built for.
            profile: the `Profile` the route built, shared with the other categories.

        Yields:
            A post per file, as `post_of` builds it.
        """
        for item in profile.feed("playlists"):
            yield post_of(site, item, "videos")

    @user.category
    def reposts(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The Reposts tab. Not `newest_first`: it is ordered by when each was reposted, not posted.

        Args:
            site: the target's site, which the posts are built for.
            profile: the `Profile` the route built, shared with the other categories.

        Yields:
            A post per file, as `post_of` builds it.
        """
        for item in profile.tab("reposts", "repost-tab"):
            yield post_of(site, item, "reposts")

    @user.category
    def liked(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The Liked tab, when the account made it public.

        Args:
            site: the target's site, which the posts are built for.
            profile: the `Profile` the route built, shared with the other categories.

        Yields:
            A post per file, as `post_of` builds it.
        """
        for item in profile.tab("liked", "liked-tab"):
            yield post_of(site, item, "liked")

    @user.category
    def favorites(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The Favorites tab, when the account made it public.

        Args:
            site: the target's site, which the posts are built for.
            profile: the `Profile` the route built, shared with the other categories.

        Yields:
            A post per file, as `post_of` builds it.
        """
        for item in profile.tab("favorites", "favorites-tab"):
            yield post_of(site, item, "favorites")

    @user.category(folder="profile", key="")
    def profile(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile picture under profile/avatar_<id>.jpg; keyed by its path.

        Args:
            site: the target's site, whose cookies go to the CDN.
            profile: the `Profile` the route built, shared with the other categories.

        Yields:
            One post with the largest picture (`avatarLarger`), or nothing when the page's
            data has none.
        """
        if not profile.open(feeds=False):
            return
        url = dig(profile.user, "user.avatarLarger")
        if url:
            # The path's last part is "<id>~<size options>.jpeg"; the id names the picture.
            picture_id = PurePosixPath(urlsplit(url).path).name.split("~")[0]
            f = File(url, name=f"avatar_{picture_id}.jpg")
            yield Post(picture_id, [f], headers=cdn_headers(site.cookies))


def page_user(data: Any) -> dict[str, Any]:
    """The profile's `userInfo` (its `user` and `stats`) from the page's data script, or {}.

    Args:
        data: the parsed JSON of the `__UNIVERSAL_DATA_FOR_REHYDRATION__` script, or None.

    Returns:
        The `userInfo` dict, or an empty dict when the data has no such path.
    """
    for key in USER_KEYS:
        data = data.get(key) if isinstance(data, dict) else None
    return data if isinstance(data, dict) else {}


def embed_posts(site: Site, video_id: str) -> Iterator[Post]:
    """The posts of one video or photo post from public embed endpoint.

    Args:
        site: the target's site; the embed page needs no browser.
        video_id: the post's id.

    Yields:
        A post per video, or one per photo of a photo post; nothing, with a warning, when the
        page failed or held no `itemStruct`.
    """
    try:
        page = site.html(site.fetch(f"{SITE}/embed/v2/{video_id}").text)
    # A failed fetch is a warning for this one post, so the rest of the run goes on. The embed
    # page is public, so a 401 or 403 there (a `LoginError` from `fetch`) is no login wall.
    except (SiteError, NetworkError, LoginError) as error:
        site.warn("{err} on embed endpoint for {id}", err=error, id=video_id)
        return
    item_struct = dig(page.script("__NEXT_DATA__"), "props.pageProps.itemStruct")
    if not item_struct:
        site.warn("Could not extract item data from embed HTML for {id}", id=video_id)
        return
    for item in items_of({"itemList": [item_struct]}):
        yield post_of(site, item, "videos")


def post_of(site: Site, item: dict[str, Any], category: str) -> Post:
    """One file of an item (a video, or one photo of a photo post) as a post.

    The file's name is given without its date (`<post>[_<n>].<ext>`), so the engine names it
    from `filename` with the date added, and keeps the extension: `.mp4` for a video and `.jpg`
    for a photo, whatever the CDN's URL ends with, as files were always named.

    Args:
        site: the target's site, whose cookies go to the CDN.
        item: one of `items_of`'s items.
        category: the category it is listed for; a `videos` item goes under videos, images or
            albums by its kind and playlist.

    Returns:
        A post of one file, with the CDN headers and the `part`, `album` and `idx` fields.
    """
    part, album = category, ""
    if category == "videos" and item["playlist"]:
        part, album = "albums", item["playlist"]
    elif category == "videos":
        part = "videos" if item["kind"] == "video" else "images"
    idx = str(item["idx"] or "")
    stem = "_".join(piece for piece in (item["id"], idx) if piece)
    name = stem + (".mp4" if item["kind"] == "video" else ".jpg")
    return Post(
        item["id"], [File(item["url"], name=name)], date=item["date"],
        headers=cdn_headers(site.cookies), part=part, album=album, idx=idx,
    )


def cdn_headers(cookies: Mapping[str, str]) -> dict[str, str]:
    """CDN headers (Referer, user agent and the session's cookies) that keep it from a 403.

    Only cookies that have a value are sent: a login with only `sid_tt` has no `sessionid`, and
    an empty `sessionid=` is no session. Without either cookie there is no Cookie header.

    Args:
        cookies: the target's cookies, by name.

    Returns:
        The headers to send with every file of the target.
    """
    headers = {"Referer": f"{SITE}/", "User-Agent": TIKTOK_UA}
    sent = [f"{name}={cookies[name]}" for name in CDN_COOKIES if cookies.get(name)]
    if sent:
        headers["Cookie"] = "; ".join(sent)
    return headers


def page_state(data: Any) -> tuple[bool, str | None] | None:
    """Whether one item-list response has a next page, and its cursor, for `site.feed(cursor=…)`.

    Every list API (`FEEDS`) answers `{"itemList": [...], "hasMore": …, "cursor": "…"}`; a body
    without `hasMore` (an error, a captcha's) says nothing.

    Args:
        data: a response's JSON.

    Returns:
        `(more, cursor)`, or None for a body that isn't a list's page.
    """
    more = has_more(data)
    if more is None:
        return None
    cursor = data.get("cursor")
    return more, str(cursor) if cursor not in (None, "") else None


def items_of(data: Any) -> list[dict[str, Any]]:
    """Media items of one TikTok API response (itemList or storyList).

    Args:
        data: a response's JSON.

    Returns:
        Dicts with `id`, `date` (the raw Unix time, see `stamp_of`), `playlist` (its name or None),
        `url`, `kind` ("video" or "image") and `idx`: 0 for a video, else the photo's place from 1.
    """
    if not isinstance(data, dict):
        return []
    items = []
    # Post list uses itemList, story list uses storyList or story_list.
    for raw in dig(data, "itemList || storyList || story_list") or []:
        if not isinstance(raw, dict):
            continue
        item_id = str(raw.get("id") or raw.get("aweme_id") or "")
        if not item_id:
            continue
        pl_name = dig(raw, "playlist.name || playlist_info.name")
        common = {"id": item_id, "date": stamp_of(raw, item_id), "playlist": pl_name}

        # A photo post has `imagePost` and no video; the two are never both read.
        images = dig(raw, "imagePost.images")
        if images:
            # A photo-mode carousel: each picture is a file of its own, numbered from 1.
            for idx, img in enumerate(images):
                path = "imageURL.urlList || displayImage.urlList || thumbnail.urlList"
                url_list = dig(img, path)
                if url_list:
                    items.append({
                        **common, "url": mirrors(url_list), "kind": "image",
                        "idx": idx + 1,
                    })
        elif video_url := best_video(raw.get("video") or {}):
            items.append({**common, "url": video_url, "kind": "video", "idx": 0})
    return items


def stamp_of(raw: dict[str, Any], item_id: str) -> int | None:
    """Post Unix time from createTime or id's upper 32 bits, or None.

    The engine turns it into a day, in UTC: a local day would name the same video differently,
    and put it on the other side of `--when`, on two machines.

    Args:
        raw: one item of a TikTok response, as its JSON object.
        item_id: the item's id, which `stamp_of` reads a time from when `createTime` is missing.

    Returns:
        Unix seconds, or None when neither the field nor the id holds a plausible time.
    """
    # A number or a string of digits; a placeholder (0, a small number) isn't a date, and the
    # post is then dated from its id instead.
    created = _unix(raw.get("createTime") or raw.get("create_time"))
    if created is not None:
        return created
    # An id is a Snowflake: the Unix time in seconds is above the lowest 32 bits.
    return _unix(int(item_id) >> 32) if item_id.isdigit() else None


def _unix(value: Any) -> int | None:
    """`value` as Unix seconds if it is a whole number (or its digits) from 2004 to now; else None.

    The upper bound allows a day of clock skew: a later time is a mistake for another number.

    Args:
        value: a number or a string of digits from a response; a bool never counts.

    Returns:
        The Unix seconds, or None when `value` is not a time in that range.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int):
        return None
    return value if EARLIEST <= value <= time.time() + 86_400 else None


def mirrors(urls: Sequence[Any]) -> str | tuple[str, ...]:
    """Every CDN address for a file as URL or mirror tuple.

        mirrors(["https://v16.example/a", "https://v19.example/a"])     # both, tried in that order

    TikTok answers 403 for some posts on one CDN host and not on another (seen 2026-11-13), so
    every address is kept and the engine tries the next one when one refuses.

    Args:
        urls: the CDN addresses a response lists, best first; empty ones and repeats are dropped.

    Returns:
        The one URL when there is one, else every URL in order, as the mirrors `File` takes.
    """
    found = tuple(dict.fromkeys(str(url) for url in urls if url))
    return found[0] if len(found) == 1 else found


def best_video(video: dict[str, Any]) -> str | tuple[str, ...] | None:
    """The URL of the video's best file with CDN mirrors: largest MP4 or playAddr.

    The whole MP4s of `bitrateInfo` are ranked by pixels, then bitrate; DASH entries are left
    out, since one saved as an MP4 has no sound. Without any, the `playAddr` is taken.

    Args:
        video: an item's `video` object; {} when the item has none.

    Returns:
        A URL or a mirror tuple, or None when the video names no file.
    """
    # Each candidate is (pixels, bitrate, url): largest picture wins, bitrate breaks tie.
    candidates = [
        (_pixels(entry), entry.get("Bitrate") or 0, m)
        for entry in video.get("bitrateInfo") or []
        if isinstance(entry, dict) and not _is_dash(entry)
        and (urls := dig(entry, "PlayAddr.UrlList"))
        and (m := mirrors(urls))
    ]
    if candidates:
        return max(candidates, key=lambda candidate: candidate[:2])[2]
    address = video.get("playAddr")
    if isinstance(address, dict):
        address = mirrors(address.get("urlList") or [])
    return address or None


def _pixels(entry: dict[str, Any]) -> int:
    """A `bitrateInfo` entry's width × height, or 0 when TikTok leaves either out.

    Args:
        entry: one `bitrateInfo` entry; its size is under `PlayAddr`.

    Returns:
        The pixel count; 0 ranks the entry last.
    """
    size = entry.get("PlayAddr") or {}
    # Sizes come as numbers, but string or null ranks file last.
    try:
        return int(size.get("Width") or 0) * int(size.get("Height") or 0)
    except (TypeError, ValueError):
        return 0


def _is_dash(entry: dict[str, Any]) -> bool:
    """Whether a `bitrateInfo` entry is a DASH stream (video only, or a manifest), not a whole MP4.

    2026-11, unconfirmed until a live check: a DASH entry is taken to be marked by `Format` "dash",
    "dash" in its `GearName` or `PlayAddr.UrlKey`, or a `.mpd` manifest URL. Neither gallery-dl nor
    yt-dlp skips DASH entries today, so there was no known rule to copy, and all four marks are
    checked to be safe: a DASH file saved as an MP4 would have no sound.

    Args:
        entry: one `bitrateInfo` entry.

    Returns:
        True when any of the four marks says DASH.
    """
    play = entry.get("PlayAddr") or {}
    marks = (
        str(entry.get("Format") or ""),
        str(entry.get("GearName") or ""),
        str(play.get("UrlKey") or ""),
    )
    urls = play.get("UrlList") or []
    return (any("dash" in mark.lower() for mark in marks)
            or any(urlsplit(str(url)).path.lower().endswith(".mpd") for url in urls))
