"""TikTok: a profile's videos, photo posts, stories, playlists, reposts, liked and favorites, and its picture.

How it works: the list APIs first, and the browser only as the fallback (plan/phase-6/simplify.md §4).
The route `user` returns a `Profile`, shared by the categories. The first category to need the
profile fetches its HTML with flnd's own client, for the user's `secUid` and id in the data the
server renders into it. Each list (`/api/post/item_list/`, the reposts, liked, favorites and
stories lists) is then asked for directly, page by page (`pages`), with the web client's fixed
parameters (gallery-dl's: `aid=1988`, `app_name=tiktok_web`, a random 19-digit `device_id`, made
anew when a page comes back repeated) and `cursor` from the last page. So a profile run needs no
page, and meets no captcha, as long as TikTok answers its lists unsigned.

When it doesn't (the HTML has no `secUid`, a list is refused or isn't JSON, or the post list comes
back empty while the profile counts videos), that list is read as before: the profile is opened in
the browser, and its JSON responses are read while the page scrolls (`watch_responses`), with one
warning. One video, by id or URL, is read from the public embed page, without a browser.

2026-11-12: TikTok's WAF answers flnd's own client with a challenge page, and in-page fetches from /explore
get the same, so a profile run falls back to the profile page and waits out its captcha.

    flnd download tik:example_creator --from videos,playlists
    flnd download https://www.tiktok.com/@example_creator/video/7301234567890123456

The site's quirks it works around:
  - TikTok shows a captcha a few seconds after the load event, over a page that looks ready. A
    person can solve it in the window; when nobody does, the source backs off as after a rate
    limit and loads the page again. A captcha can also cover the grid later, so no video ever
    comes: that is told from a profile with no videos by looking for the captcha.
  - /api/post/item_list lists the videos and the photo posts, newest first, and says in `hasMore`
    when it has ended. A video of a playlist carries the playlist's name in it, so one response
    holds the `videos` and the `playlists`, which are told apart by it.
  - The Reposts, Favorites and Liked tabs are opened by a click and read the same way. Liked and
    Favorites show only when the account made them public; otherwise one line says so.
  - Full MP4 clips and photo-mode carousels are saved. A missing createTime is read from the id
    itself (a Snowflake: the upper 32 bits).
  - Its CDN sends 403 Forbidden to a request without tt_chain_token, sessionid and the Referer.
  - When the API stays walled, each post linked on the page is read from its embed page instead.

Folder layout, under `.flnd/media/tik/<username>/`:
  videos/<YYYY-MM-DD>_<video_id>.mp4
  images/<YYYY-MM-DD>_<post_id>_<idx>.jpg
  stories/<YYYY-MM-DD>_<story_id>.mp4 (or .jpg)
  albums/<playlist_title>/<YYYY-MM-DD>_<video_id>.mp4
  reposts/, liked/, favorites/   <YYYY-MM-DD>_<video_id>.mp4 (or _<idx>.jpg)
  profile/avatar_<id>.jpg
A single video's folder, `.flnd/media/tik/<video_id>/`, has `videos/` or `images/` in it.
"""

from __future__ import annotations

import random
import re
from collections import deque
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, Iterator, List, Mapping, NamedTuple, Sequence
from urllib.parse import urlsplit

from flnd.source import FetchError, File, Post, Site, Source, day, fill, pages, target
from flnd.sources.common.watch import Responses, watch_responses
from shared import http, selector

# The user agent TikTok's embed page and CDN have always been sent: a desktop Chrome.
TIKTOK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/128.0.0.0 Safari/537.36")

# Captcha verification detection
CAPTCHA_CSS = ('#captcha-verify-container-main-page, #captcha_container, [id*="captcha-verify"], '
               '[class*="captcha_verify"], [class*="captcha-verify"]')
CAPTCHA_TEXT = re.compile(r"Drag the slider|Drag the puzzle|Verify to continue|Select 2 objects", re.IGNORECASE)
# The page's visible text: scripts and styles hold strings no reader sees.
VISIBLE_TEXT = "//body//text()[not(ancestor::script) and not(ancestor::style)]"
POST_LINKS = 'a[href*="/video/"]::attr(href), a[href*="/photo/"]::attr(href)'
# Seconds to let the profile settle before looking for a captcha, and to wait for a tab to take a click.
SETTLE = 5.0
CLICK_TIMEOUT = 10.0
# Seconds a person gets to solve a captcha before the source backs off instead.
CAPTCHA_TIMEOUT = 60.0
# The profile's data, server-rendered into the page.
PAGE_DATA = "__UNIVERSAL_DATA_FOR_REHYDRATION__"
USER = '"__DEFAULT_SCOPE__"."webapp.user-detail".userInfo'
SITE = "https://www.tiktok.com"
# The web client's fixed parameters, which its list APIs answer without a signature (gallery-dl's,
# read 2026-11-12; to be confirmed by a live run). `device_id` is added per page, `cursor` and the
# list's own (`secUid`, `authorId`) by the caller.
WEB_PARAMS = {"aid": "1988", "app_name": "tiktok_web", "app_language": "en", "browser_language": "en-US",
              "browser_name": "Mozilla", "browser_online": "true", "browser_platform": "Win32",
              "channel": "tiktok_web", "cookie_enabled": "true", "device_platform": "web_pc", "focus_state": "true",
              "from_page": "user", "is_fullscreen": "false", "is_page_visible": "true", "language": "en",
              "os": "windows", "region": "US", "screen_height": "1080", "screen_width": "1920"}
# Items asked for per page: what the web client asks for.
PAGE_SIZE = 35
# A web client's `device_id` is 19 random digits.
DEVICE_DIGITS = 19


class Feed(NamedTuple):
    """One list the profile loads: its category, and the API path its pages come from."""

    # The category the list fills: "videos", "stories", "reposts", "favorites" or "liked".
    category: str
    # The API path its pages come from, without the host; `is_feed_of` matches responses by it.
    api: str


# The list each category asks for directly, and which of the user's ids it takes.
LISTS = {
    "videos": ("/api/post/item_list/", "secUid"),
    "stories": ("/api/story/item_list/", "authorId"),
    "reposts": ("/api/repost/item_list/", "secUid"),
    "favorites": ("/api/user/collect/item_list/", "secUid"),
    "liked": ("/api/favorite/item_list/", "secUid"),
}


class Refused(Exception):
    """A list API that didn't answer with a list: the category reads it in the browser instead."""


def new_device() -> str:
    """A web client's `device_id`: 19 random digits, which TikTok ties a session's pages to."""
    return str(random.randrange(10 ** (DEVICE_DIGITS - 1), 10 ** DEVICE_DIGITS))


def list_pages(site: Site, category: str, user: Mapping[str, Any]) -> Iterator[Dict[str, Any]]:
    """Each page of one of the profile's lists, asked for directly, until it says it has no more.

        for data in list_pages(site, "videos", {"secUid": "MS4w…", "id": "6800000000000000000"}): ...

    Args:
        site: the target's site; each page is paced, a 429 waited out.
        category: a key of `LISTS`.
        user: the profile's `user` object, holding its `secUid` and `id`.

    Raises:
        Refused: a page was refused, isn't JSON, or has a `statusCode` other than 0; the caller
            reads the list in the browser instead.

    A page that only repeats items already seen gets a new `device_id` and is asked once more, as
    gallery-dl does; a second repeat ends the list, and so does a cursor seen before (`pages`).
    """
    path, key = LISTS[category]
    value = user.get("secUid") if key == "secUid" else user.get("id")
    if not value:
        raise Refused(f"the profile's data has no {key}")
    # A one-item list, so `ask` can swap in a new device id for a repeated page without a `nonlocal`.
    device = [new_device()]
    # Every id listed so far, to tell a page of old items from a page with new ones.
    seen: set[str] = set()

    def ask(cursor: str | None) -> tuple[Dict[str, Any], set[str]]:
        params = {**WEB_PARAMS, key: str(value), "count": str(PAGE_SIZE), "cursor": cursor or "0",
                  "device_id": device[0]}
        data = list_page(site, f"{SITE}{path}", params)
        return data, {item["id"] for item in items_of(data)}

    def fetch(cursor: str | None) -> Dict[str, Any]:
        data, ids = ask(cursor)
        # A page of only seen items is retried once with a new device, as gallery-dl does, before the list is trusted to end.
        if cursor and ids and ids <= seen:
            device[0] = new_device()
            data, ids = ask(cursor)
            if ids <= seen:
                # Repeated again: the list has nothing new to give, so it ends here.
                return {**data, "hasMore": False}
        seen.update(ids)
        return data

    yield from pages(fetch, lambda data: str(data["cursor"]) if data.get("hasMore") and data.get("cursor") else None)


def list_page(site: Site, url: str, params: Mapping[str, str]) -> Dict[str, Any]:
    """One page of a list API, as its JSON object; a refusal of any kind raises `Refused`.

    Args:
        site: the target's site, which paces the request and waits out a 429.
        url: the list API's full URL, without the query.
        params: the query string: the web client's fixed parameters, the list's own id, `count`,
            `cursor` and `device_id`.

    Returns:
        The page's JSON object.

    Raises:
        Refused: the site fails the request, the answer isn't JSON or isn't an object, or it
            carries an error `statusCode`; the caller reads the list in the browser instead.
    """
    # A failure the site raises is a refusal of this list, not a crash: the list is read in the browser.
    try:
        response = site.fetch(url, params=params, headers={"Referer": f"{SITE}/"})
    except FetchError as error:
        raise Refused(str(error)) from None
    try:
        data = response.json
    # 2026-11 (gallery-dl #9718): an unsigned list can come back as an empty 200 body.
    except http.Error:
        raise Refused("its answer isn't JSON") from None
    if not isinstance(data, dict):
        raise Refused("its answer isn't a list")
    # TikTok reports a failure inside a 200 response, as `statusCode`; some pages spell it `status_code`.
    status = data.get("statusCode", data.get("status_code", 0))
    if status not in (0, None):
        raise Refused(f"statusCode {status}")
    return data


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
    """A match for the responses of the feeds listing `category`, as `watch_responses` takes it.

    Args:
        category: a feed's category: "videos", "stories", "reposts", "favorites" or "liked".
    """
    apis = [feed.api for feed in FEEDS if feed.category == category]
    return lambda url: any(api in url for api in apis)


def has_captcha(site: Site) -> bool:
    """Whether the page shows a captcha: its container, or its words in the text a reader sees.

    Args:
        site: the target's site, whose open page is checked.
    """
    page = site.page.document
    return bool(page.css(CAPTCHA_CSS)) or bool(CAPTCHA_TEXT.search(" ".join(page.xpath(VISIBLE_TEXT).all)))


class Profile:
    """What a profile's categories share: its data, its lists, and its page, opened only when a list needs it.

        profile = Profile(site, "example_creator")
        for item in profile.feed("videos"): ...   # the post list, direct or else from the page, once for all

    Args:
        site: the target's site, whose tab is the profile's.
        name: the user name, without `@`.

    The route builds it and asks nothing yet. The first category to need the profile reads its
    HTML directly (`described`); when that gives no `secUid`, every list is read in the browser as
    before routes: the response watchers must exist before the page loads or the first responses
    are lost, so the first category that needs the page opens it, as it needs it.
    """

    def __init__(self, site: Site, name: str):
        """Set the profile's URL and empty state; nothing is fetched until a category asks for it."""
        self.site = site
        self.url = f"{SITE}/@{name}"
        # The profile's own data (its `user` with `secUid` and `id`, its `stats`); empty until read.
        self.user: Dict[str, Any] = {}
        # How many items each feed gave, saved or not, so a second run still sees a list grow.
        self.found = {feed.category: 0 for feed in FEEDS}
        self._direct: bool | None = None
        self._loaded: bool | None = None
        # The post list's pages: from the API directly, or else from the page, scrolling for more;
        # and the stories' page responses (no scrolling: they come with the load).
        self._api: Iterator[Dict[str, Any]] | None = None
        self._posts: Responses | None = None
        self._stories: Responses | None = None
        self._started = False
        # The post feed's items waiting for the category that wants them: a playlist's or not.
        self._queues: Dict[str, deque[Dict[str, Any]]] = {"videos": deque(), "playlists": deque()}
        # The (id, idx) of each post-feed file queued, so a page re-read after a fallback adds nothing twice.
        self._seen: set[tuple[str, int]] = set()
        self._told = False

    @property
    def on_page(self) -> bool:
        """Whether the profile's page was opened in the browser."""
        return bool(self._loaded)

    def described(self) -> bool:
        """Whether the profile's HTML, fetched directly on the first call, gave its data with a `secUid`."""
        if self._direct is None:
            # Set before the fetch: a failure leaves it False, so later calls answer without fetching again.
            self._direct = False
            try:
                page = self.site.fetch(self.url).html
            # The HTML can't be fetched or parsed; the profile is then read in the browser, as before.
            except (FetchError, http.Error):
                return False
            user = page.script(PAGE_DATA).jmespath(USER).first
            if isinstance(user, dict) and selector.parse(user).jmespath("user.secUid").first:
                self.user, self._direct = user, True
        return self._direct

    def open(self, *, feeds: bool) -> bool:
        """Open the profile on the first call, and return whether it loaded; later calls answer the same.

        Args:
            feeds: whether to read the post and story feeds from this load. Only the first call
                decides: the categories that read them are declared before the ones that don't,
                so the first to need the page is one of them whenever any is wanted.
        """
        if self._loaded is None:
            if feeds:
                # Watching starts before the page loads: the first responses come during the load.
                self._posts = watch_responses(self.site, is_feed_of("videos"), scroll=True)
                self._stories = watch_responses(self.site, is_feed_of("stories"))
            self._loaded = self._load()
        return self._loaded

    def start(self) -> bool:
        """Start the post feed on the first call: its list API when the profile was described, else the page."""
        if not self._started:
            self._started = True
            if self.described():
                self._api = list_pages(self.site, "videos", self.user.get("user") or {})
            else:
                return self.open(feeds=True)
        return self._api is not None or bool(self._loaded)

    def feed(self, wanted: str) -> Iterator[Dict[str, Any]]:
        """The post feed's items of `wanted` ("videos", or "playlists" for those in a playlist), reading the list as needed.

        Reading gives both kinds at once; the kind not asked for waits in its queue for the
        category that is. The iterator ends when the feed does.

        Args:
            wanted: "videos" or "playlists": which queue to take items from.
        """
        queue = self._queues[wanted]
        while True:
            if queue:
                yield queue.popleft()
            elif not self._read():
                return

    def stories(self) -> Iterator[Dict[str, Any]]:
        """The profile's stories: from the page when it was loaded watching them, else from their list API."""
        if self._stories is None and self.described():
            try:
                for data in list_pages(self.site, "stories", self.user.get("user") or {}):
                    items = items_of(data)
                    self.found["stories"] += len(items)
                    yield from items
                return
            except Refused as error:
                self.site.warn("stories: the list API refused ({error}); reading the profile in the browser",
                               error=error)
                if not self._watch(stories=True):
                    return
        elif self._stories is None and not self.open(feeds=True):
            return
        stories, self._stories = self._stories, None
        for data in stories or ():
            items = items_of(data)
            self.found["stories"] += len(items)
            yield from items

    def tab(self, category: str, tab: str) -> Iterator[Dict[str, Any]]:
        """One of the profile's other lists, to its end: its API, else its tab in the browser.

        Args:
            category: the tab's feed: "reposts", "favorites" or "liked".
            tab: the tab's `data-e2e` name.

        One line says when the account doesn't show the list, or shows nothing in it.
        """
        # The items the API yielded, so a browser fallback that re-reads the tab doesn't yield them twice.
        seen: set[tuple[str, int]] = set()
        if self.described():
            try:
                for data in list_pages(self.site, category, self.user.get("user") or {}):
                    for item in items_of(data):
                        seen.add((item["id"], item["idx"]))
                        self.found[category] += 1
                        yield item
                if not self.found[category]:
                    self.site.info("{tab}: nothing public on this account", tab=category)
                return
            except Refused as error:
                self.site.warn("{tab}: the list API refused ({error}); reading the tab in the browser",
                               tab=category, error=error)
        if not self.open(feeds=False):
            return
        for item in self._clicked(category, tab):
            if (item["id"], item["idx"]) not in seen:
                yield item

    def wait_out_captcha(self) -> None:
        """Until no captcha is left: a person solves it in the window, or the source backs off and reloads."""
        while has_captcha(self.site):
            if self.site.handoff(until=lambda: not has_captcha(self.site), timeout=CAPTCHA_TIMEOUT,
                                 message="Solve the TikTok captcha in the browser window"):
                return
            # A captcha nobody solved in time is handled as a rate limit: back off, then load the page again.
            self.site.back_off("TikTok captcha")
            self.site.goto(self.url)
            self.site.page.raw.wait_for_timeout(SETTLE * 1000)

    def _clicked(self, category: str, tab: str) -> Iterator[Dict[str, Any]]:
        """Open one profile tab in the browser by a click and read its feed to the end."""
        css = f'[data-e2e="{tab}"]'
        # Liked and Favorites are on the page only when the account made them public.
        if not self.site.page.document.css(css):
            self.site.info("{tab}: not public on this account", tab=category)
            return
        # Subscribed before the click, which is what makes the page ask for the list.
        with watch_responses(self.site, is_feed_of(category), scroll=True) as responses:
            try:
                self.site.page.raw.locator(css).first.click(timeout=CLICK_TIMEOUT * 1000)
            # Playwright's errors can't be named here (sources never import it); a tab that won't open is skipped.
            except Exception as error:
                self.site.warn("{tab}: the tab didn't open: {error}", tab=category,
                               error=str(error).splitlines()[0])
                return
            for data in responses:
                items = items_of(data)
                self.found[category] += len(items)
                yield from items
        if not self.found[category]:
            self.site.info("{tab}: nothing public on this account", tab=category)

    def _watch(self, *, posts: bool = False, stories: bool = False) -> bool:
        """Read a list in the browser after its API failed: the first load watches both feeds, a later one reloads."""
        if self._loaded is None:
            return self.open(feeds=True)
        if posts:
            self._posts = watch_responses(self.site, is_feed_of("videos"), scroll=True)
        if stories:
            self._stories = watch_responses(self.site, is_feed_of("stories"))
        self._loaded = self._load()
        return self._loaded

    def _load(self) -> bool:
        """Load the profile, wait out any captcha it shows and read its own data, and return True.

        Raises:
            FetchError: the profile didn't load. Raised, not returned: `videos` would end as if
                listed to its end and be recorded complete (fixes #3).
        """
        if not self.site.goto(self.url):
            raise FetchError("the profile didn't load")
        # The captcha shows a few seconds after the load event, over a page that looks ready.
        self.site.page.raw.wait_for_timeout(SETTLE * 1000)
        self.wait_out_captcha()
        # A reload that finds no data keeps what an earlier load read, rather than forgetting the secUid.
        self.user = self.site.page.document.script(PAGE_DATA).jmespath(USER).first or self.user or {}
        return True

    def _read(self) -> bool:
        """Read the post feed's next page into the queues; False once the feed has ended."""
        if not self.start():
            return False
        # Step 1: the list API, page by page, for as long as it answers.
        if self._api is not None:
            try:
                data = next(self._api)
            except StopIteration:
                self._api = None
                listed = selector.parse(self.user).jmespath("stats.videoCount").first
                # 2026-11 (gallery-dl #9718): an empty first page while the profile counts videos is a wall, not the end.
                if self.found["videos"] or not listed:
                    self._end()
                    return False
                self.site.warn("videos: the list API gave none of the {listed} videos; reading the profile "
                               "in the browser", listed=listed)
            except Refused as error:
                self._api = None
                self.site.warn("videos: the list API refused ({error}); reading the profile in the browser",
                               error=error)
            else:
                self._file(data, api=True)
                return True
            if not self._watch(posts=True):
                return False
        # Step 2: the browser's watched responses, which arrive as the page scrolls.
        while self._posts is not None:
            for data in self._posts:
                self._file(data)
                return True
            # The scroll ended. No video at all, with a captcha over the grid, means it never loaded:
            # wait the captcha out, and scroll again. The new watcher comes first, before a reload.
            if self.found["videos"] or not has_captcha(self.site):
                self._end()
                return False
            self._posts = watch_responses(self.site, is_feed_of("videos"), scroll=True)
            self.wait_out_captcha()
        return False

    def _file(self, data: Any, *, api: bool = False) -> None:
        """Queue one post-feed page's items not queued before, and end a watched feed when it says it has no more."""
        items = items_of(data)
        self.found["videos"] += len(items)
        for item in items:
            if (item["id"], item["idx"]) in self._seen:
                continue
            self._seen.add((item["id"], item["idx"]))
            self._queues["playlists" if item["playlist"] else "videos"].append(item)
        # Only a watched page can end the feed by `hasMore`; an API page ends it by running out (`StopIteration`).
        if not api and isinstance(data, dict) and "hasMore" in data and not data["hasMore"] and self._posts is not None:
            self._end()

    def _end(self) -> None:
        """Stop watching the post feed, and say once how many videos it gave against how many the profile lists."""
        if self._posts is not None:
            self._posts.close()
            self._posts = None
        if not self._told:
            self._told = True
            listed = selector.parse(self.user).jmespath("stats.videoCount").first
            self.site.info("videos: {n} found; the profile lists {listed}", n=self.found["videos"], listed=listed)


class TikTok(Source):
    """TikTok profiles in a browser, and single videos from the embed page."""

    code = "tik"
    name = "TikTok"
    # The short-link hosts, vm. and vt.tiktok.com, are subdomains of this one: a short link is read by
    # the engine, which follows it once when no route matches.
    domains = ("tiktok.com",)
    needs = ("sessionid", "sid_tt")
    user_agent = TIKTOK_UA
    # No known limit; a captcha is its signal, which doubles the gap (plan/phase-6/pace.md).
    pace = 1.0
    # What a post's files are named and keyed by, as they always were: `<date>_<id>` for a video and
    # `<date>_<id>_<n>` for the n-th photo (n from 1), the key the same without the date. `idx` is
    # empty for a video, so the `_` before it goes. A post's `part` is its folder (videos, images,
    # a category, or albums), and its `album` the playlist, under albums.
    folder = "{part}/{album}"
    filename = "{date}_{id}_{idx}"
    key = "{id}_{idx}"

    # `video` comes before `user`, so a 16-digit id is never a user name. A video or photo URL with
    # no user before it is read too, as the old reader did.
    @target(r"{id:\d{16,}}", r"tiktok.com/*/video/{id:\d+}", r"tiktok.com/*/photo/{id:\d+}",
            r"tiktok.com/video/{id:\d+}", r"tiktok.com/photo/{id:\d+}")
    def video(self, site: Site, id: str) -> Iterator[Post]:
        """One video or photo post, by its id: `Target("tik", "video", "7301234567890123456")`.

        Args:
            site: the target's site; the embed page needs no browser.
            id: the post's numeric id, from the target or the URL.
        """
        yield from embed_posts(site, id)

    # The link's `@` is written in the pattern, so its regex is spelled out: a default `{user}` would
    # drop a second `@`, and `tiktok.com/@@name` is refused as it always was.
    @target("{user}", r"tiktok.com/@{user:[\w.]+}")
    def user(self, site: Site, user: str) -> Profile:
        """A profile, by its user name; a `@` in front of it is dropped. Nothing is opened yet.

        Args:
            site: the target's site, whose tab the profile is opened in.
            user: the user name, without `@`.
        """
        return Profile(site, user)

    # Newest first, apart from pinned videos at the top, which the engine's early stop allows for.
    @user.category(newest_first=True)
    def videos(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The videos and photo posts that are in no playlist; the page's links when every list is walled.

        Args:
            site: the target's site, whose page is read for links when the API gave nothing.
            profile: the `Profile` the route built, shared with the other categories.
        """
        for item in profile.feed("videos"):
            yield post_of(site, item, "videos")
        if not profile.found["videos"] and profile.on_page:
            # A walled API gave nothing: read each post linked on the page from its embed page.
            links = [match.group(2) for href in site.page.document.css(POST_LINKS).all
                     if (match := re.search(r"/(video|photo)/(\d+)", href))]
            for video_id in dict.fromkeys(links):
                yield from embed_posts(site, video_id)

    @user.category
    def stories(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile's stories: their list API, or the page they come with.

        Args:
            site: the target's site, which the stories' posts are built for.
            profile: the `Profile` the route built, shared with the other categories.
        """
        for item in profile.stories():
            yield post_of(site, item, "stories")

    @user.category(newest_first=True)
    def playlists(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The videos that are in a playlist, each under `albums/<playlist title>/`.

        Args:
            site: the target's site, which the posts are built for.
            profile: the `Profile` the route built, shared with the other categories.
        """
        for item in profile.feed("playlists"):
            yield post_of(site, item, "videos")

    @user.category
    def reposts(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The Reposts tab. Not `newest_first`: it is ordered by when each was reposted, not posted.

        Args:
            site: the target's site, which the posts are built for.
            profile: the `Profile` the route built, shared with the other categories.
        """
        for item in profile.tab("reposts", "repost-tab"):
            yield post_of(site, item, "reposts")

    @user.category
    def liked(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The Liked tab, when the account made it public.

        Args:
            site: the target's site, which the posts are built for.
            profile: the `Profile` the route built, shared with the other categories.
        """
        for item in profile.tab("liked", "liked-tab"):
            yield post_of(site, item, "liked")

    @user.category
    def favorites(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The Favorites tab, when the account made it public.

        Args:
            site: the target's site, which the posts are built for.
            profile: the `Profile` the route built, shared with the other categories.
        """
        for item in profile.tab("favorites", "favorites-tab"):
            yield post_of(site, item, "favorites")

    @user.category(folder="profile", key="")
    def profile(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile picture, named `avatar_<picture id>.jpg` and keyed by that path, as always.

        Args:
            site: the target's site, whose cookies the picture's CDN wants.
            profile: the `Profile` the route built; its page is opened only when the HTML gave no `secUid`.
        """
        if not profile.described() and not profile.open(feeds=False):
            return
        url = selector.parse(profile.user).jmespath("user.avatarLarger").first
        if url:
            # The path's last part is "<id>~<size options>.jpeg"; the id names the picture.
            picture_id = PurePosixPath(urlsplit(url).path).name.split("~")[0]
            yield Post(picture_id, [File(url, name=f"avatar_{picture_id}.jpg")], headers=cdn_headers(site.cookies))


def embed_posts(site: Site, video_id: str) -> Iterator[Post]:
    """The posts of one video or photo post, read from TikTok's public embed endpoint; a warning when it can't be.

    Args:
        site: the target's site; the embed page needs no browser.
        video_id: the post's id.
    """
    # No redirects: a redirect lands on a page with no item to read, so it is reported as a miss below.
    # Seconds for one embed page, set by flnd rather than by the site.
    try:
        page = site.fetch(f"https://www.tiktok.com/embed/v2/{video_id}", timeout=20.0, follow_redirects=False).html
    # A failed fetch is a warning for this one post, so the rest of the run goes on.
    except FetchError as error:
        site.warn("{err} on embed endpoint for {id}", err=error, id=video_id)
        return
    data = page.script("__NEXT_DATA__").first
    item_struct = selector.parse(data).jmespath("props.pageProps.itemStruct").first if data else None
    if not item_struct:
        site.warn("Could not extract item data from embed HTML for {id}", id=video_id)
        return
    for item in items_of({"itemList": [item_struct]}):
        yield post_of(site, item, "videos")


def post_of(site: Site, item: Dict[str, Any], category: str) -> Post:
    """One file of an item as a post: its folder from the category, its name and key as they always were.

    Args:
        site: the target's site, whose cookies the CDN wants.
        item: one of `items_of`'s.
        category: the feed's category. A video of the `videos` feed lands in `videos/` or `images/`,
            or in `albums/<playlist>/` when it has a playlist; any other feed in its own folder.

    The file is named here, with the class's own format: the CDN's URLs have no extension and a
    photo's says `.jpeg`, while a video has always been an `.mp4` and a photo a `.jpg`.
    """
    part, album = category, ""
    if category == "videos" and item["playlist"]:
        part, album = "albums", item["playlist"]
    elif category == "videos":
        part = "videos" if item["kind"] == "video" else "images"
    idx = item["idx"] or ""
    values = {"date": day(item["date"]), "id": item["id"], "idx": idx}
    name = fill(TikTok.filename, values) + (".mp4" if item["kind"] == "video" else ".jpg")
    return Post(item["id"], [File(item["url"], name=name)], date=item["date"], headers=cdn_headers(site.cookies),
                part=part, album=album, idx=idx)


def cdn_headers(cookies: Mapping[str, str]) -> Dict[str, str]:
    """What TikTok's CDN wants to not answer 403 Forbidden: the Referer and the session cookies (tt_chain_token).

    Args:
        cookies: the target's cookies, by name.
    """
    token = cookies.get("tt_chain_token", "")
    sess = cookies.get("sessionid", "")
    # The CDN's cookie carries tt_chain_token only when the session has one; sessionid is always sent.
    cookie_header = f"tt_chain_token={token}; sessionid={sess}" if token else f"sessionid={sess}"
    return {"Referer": "https://www.tiktok.com/", "User-Agent": TIKTOK_UA, "Cookie": cookie_header}


def items_of(data: Any) -> List[Dict[str, Any]]:
    """The media items of one TikTok API response (`itemList` or `storyList`): one per video, one per photo.

    Args:
        data: a response's JSON.

    Returns:
        Dicts with `id`, `date` (the raw Unix time, see `stamp_of`), `playlist` (its name or None),
        `url`, `kind` ("video" or "image") and `idx`: 0 for a video, else the photo's place from 1.
    """
    if not isinstance(data, dict):
        return []
    items = []
    # The post list says `itemList`, the story list `storyList`, and some older responses `story_list`.
    for raw in selector.parse(data).jmespath("itemList || storyList || story_list").all:
        item_id = str(raw.get("id") or raw.get("aweme_id") or "")
        if not item_id:
            continue
        common = {"id": item_id, "date": stamp_of(raw, item_id),
                  "playlist": selector.parse(raw).jmespath("playlist.name || playlist_info.name").first}

        # A photo post has `imagePost` and no video; the two are never both read.
        images = selector.parse(raw).jmespath("imagePost.images").all
        if images:
            # A photo-mode carousel: each picture is a file of its own, numbered from 1.
            for idx, img in enumerate(images):
                url_list = selector.parse(img).jmespath("imageURL.urlList || displayImage.urlList || thumbnail.urlList").all
                if url_list:
                    items.append({**common, "url": mirrors(url_list), "kind": "image", "idx": idx + 1})
        elif video_url := best_video(raw.get("video") or {}):
            items.append({**common, "url": video_url, "kind": "video", "idx": 0})
    return items


def stamp_of(raw: Dict[str, Any], item_id: str) -> Any:
    """The post's Unix time from createTime, or else from the id's upper 32 bits; None when neither is one.

    The engine turns it into a day, in UTC (`day`), as twt and tlg do: a local day would name the
    same video differently, and put it on the other side of `--since`, on two machines (fix #15, 2026-11).

    Args:
        raw: one item of a TikTok response, as its JSON object.
        item_id: the item's id, which `stamp_of` reads a time from when `createTime` is missing.
    """
    create_time = raw.get("createTime") or raw.get("create_time")
    # A number or a string of digits; a placeholder (0, a small number) isn't a date to `day`, and
    # the post is then dated from its id instead.
    if day(create_time) is not None:
        return create_time
    # An id is a Snowflake: the Unix time in seconds is above the lowest 32 bits.
    stamp = int(item_id) >> 32 if item_id.isdigit() else None
    return stamp if day(stamp) is not None else None


def mirrors(urls: Sequence[Any]) -> str | tuple[str, ...]:
    """Every CDN address TikTok gives for one file, as `File.url` takes them: one URL, or the mirrors in order.

        mirrors(["https://v16.example/a", "https://v19.example/a"])     # both, tried in that order

    TikTok answers 403 for some posts on one CDN host and not on another, so every address is kept
    and `shared.http.download` tries the next one when one refuses (fixes #8).
    """
    found = tuple(dict.fromkeys(str(url) for url in urls if url))
    return found[0] if len(found) == 1 else found


def best_video(video: Dict[str, Any]) -> str | tuple[str, ...] | None:
    """The URL of the video's best file, with its other CDN addresses: the largest MP4 of `bitrateInfo`, else `playAddr`.

        url = best_video(item["video"])   # "https://v16-webapp.tiktok.com/…"

    Args:
        video: an item's `video` object from TikTok's web API.

    Returns:
        The URL of the `bitrateInfo` entry with the most pixels (width × height), then the highest
        `Bitrate`, skipping DASH entries; else `playAddr`; else None, and the post has no video to save.

    `downloadAddr` is never used, even when it is all there is: it is TikTok's copy with the
    watermark and the closing card burnt in, so it isn't the post as published.

    The ranking by pixels, with `playAddr` as the fallback and never `downloadAddr`, follows
    gallery-dl. The DASH skip and the `Bitrate` tie-break are flnd's own (fix #16, 2026-11), and
    `_is_dash`'s marks stay unconfirmed until a live check.
    """
    # Each candidate is (pixels, bitrate, url): the largest picture wins, and the bitrate breaks a tie.
    candidates = [(_pixels(entry), entry.get("Bitrate") or 0, mirrors(urls)) for entry in video.get("bitrateInfo") or []
                  if isinstance(entry, dict) and not _is_dash(entry)
                  and (urls := selector.parse(entry).jmespath("PlayAddr.UrlList").all)]
    if candidates:
        return max(candidates, key=lambda candidate: candidate[:2])[2]
    address = video.get("playAddr")
    if isinstance(address, dict):
        address = mirrors(address.get("urlList") or [])
    return address or None


def _pixels(entry: Dict[str, Any]) -> int:
    """A `bitrateInfo` entry's width × height, or 0 when TikTok leaves either out."""
    size = entry.get("PlayAddr") or {}
    # 2026-11: sizes come as numbers, but a string or null must only rank the file last, not fail the post.
    try:
        return int(size.get("Width") or 0) * int(size.get("Height") or 0)
    except (TypeError, ValueError):
        return 0


def _is_dash(entry: Dict[str, Any]) -> bool:
    """Whether a `bitrateInfo` entry is a DASH stream (video only, or a manifest), not a whole MP4.

    2026-11, unconfirmed until a live check: a DASH entry is taken to be marked by `Format` "dash",
    "dash" in its `GearName` or `PlayAddr.UrlKey`, or a `.mpd` manifest URL. Neither gallery-dl nor
    yt-dlp skips DASH entries today, so there was no known rule to copy, and all four marks are
    checked to be safe: a DASH file saved as an MP4 would have no sound.
    """
    play = entry.get("PlayAddr") or {}
    marks = (str(entry.get("Format") or ""), str(entry.get("GearName") or ""), str(play.get("UrlKey") or ""))
    urls = play.get("UrlList") or []
    return (any("dash" in mark.lower() for mark in marks)
            or any(urlsplit(str(url)).path.lower().endswith(".mpd") for url in urls))
