"""TikTok: profile videos, photo posts, stories, playlists, reposts, liked, and favorites.

Every profile feed is read in the browser with `watch_responses`, streaming items while
scrolling to the end. Captchas trigger back-off and reload.

One video, by id or URL, is read from the public embed page without a browser.

Folder layout under `.flnd/media/tik/<username>/`:
  videos/, stories/, reposts/, ...   <date>_<id>[_<n>].<ext>
  playlists/<name>/                  <date>_<id>[_<n>].<ext>
  avatar/                            avatar_<id>.jpg
"""

from __future__ import annotations

import re
from collections import deque
from datetime import date
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, Generator, Iterator, List, Mapping, NamedTuple, Sequence
from urllib.parse import urlsplit

from flnd.source import (
    Blocked, File, NetworkError, Post, Site, SiteError, Source, caught_up, day, fill, target,
)
from flnd.sources.common.feed import Responses, watch_responses
from flnd import _html as selector

# The user agent TikTok's embed page and CDN have always been sent: a desktop Chrome.
TIKTOK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/128.0.0.0 Safari/537.36")

# Captcha verification detection
CAPTCHA_ATTEMPTS = 3
CAPTCHA_CSS = ('#captcha-verify-container-main-page, #captcha_container, [id*="captcha-verify"], '
               '[class*="captcha_verify"], [class*="captcha-verify"]')
CAPTCHA_TEXT = re.compile(
    r"Drag the slider|Drag the puzzle|Verify to continue|Select 2 objects", re.IGNORECASE,
)
# The page's visible text: scripts and styles hold strings no reader sees.
VISIBLE_TEXT = "//body//text()[not(ancestor::script) and not(ancestor::style)]"
POST_LINKS = 'a[href*="/video/"]::attr(href), a[href*="/photo/"]::attr(href)'
# Seconds to let profile settle before looking for captcha or tab click.
SETTLE = 5.0
CLICK_TIMEOUT = 10.0
# Seconds a person gets to solve a captcha before the source backs off instead.
CAPTCHA_TIMEOUT = 60.0
# The profile's data, server-rendered into the page.
PAGE_DATA = "__UNIVERSAL_DATA_FOR_REHYDRATION__"
USER = '"__DEFAULT_SCOPE__"."webapp.user-detail".userInfo'
SITE = "https://www.tiktok.com"
# Consecutive posts older than `since` before stopping scroll early (covers pinned videos).
OLD_STREAK = 5


class Feed(NamedTuple):
    """One list the profile loads: its category, and the API path its pages come from."""

    # The category the list fills: "videos", "stories", "reposts", "favorites" or "liked".
    category: str
    # The API path its pages come from, without the host; `is_feed_of` matches responses by it.
    api: str


def _hit_saved(items: list[dict[str, Any]], wanted: str) -> bool:
    """Whether `videos` items hit the 5-saved-post early-stop streak."""
    if wanted != "videos":
        return False
    return any(
        not item["playlist"] and caught_up(item["id"], posted=item.get("date"))
        for item in firsts(items)
    )


def firsts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first item of each post: a photo post gives one item per photo, all with its id."""
    found: dict[str, dict[str, Any]] = {}
    for item in items:
        found.setdefault(item["id"], item)
    return list(found.values())


def older_streak(items: list[dict[str, Any]], since: date | None, streak: int) -> int:
    """Count consecutive items older than `since`, carrying `streak` forward."""
    if since is None:
        return streak
    for item in firsts(items):
        posted = day(item.get("date"))
        if posted is None:
            continue
        streak = streak + 1 if posted < since else 0
    return streak


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
    has_captcha_css = bool(page.css(CAPTCHA_CSS))
    visible = " ".join(page.xpath(VISIBLE_TEXT).get())
    return has_captcha_css or bool(CAPTCHA_TEXT.search(visible))


class Profile:
    """Profile shared state: data, lists, page opened on demand.

        profile = Profile(site, "example_creator")
        for item in profile.feed("videos"): ...   # post list, direct or page

    Args:
        site: the target's site, whose tab is the profile's.
        name: the user name, without `@`.

    The route builds it and asks nothing yet. The first category to need the profile reads its
    HTML directly (`described`); when that gives no `secUid`, every list is read in the browser as
    before routes: the response watchers must exist before the page loads or the first responses
    are lost, so the first category that needs the page opens it, as it needs it.
    """

    def __init__(self, site: Site, name: str):
        """Set profile URL and initial empty state."""
        self.site = site
        self.url = f"{SITE}/@{name}"
        # The profile's own data (its `user` with `secUid` and `id`, its `stats`); empty until read.
        self.user: Dict[str, Any] = {}
        # How many items each feed gave, saved or not, so a second run still sees a list grow.
        self.found = {feed.category: 0 for feed in FEEDS}
        self._loaded: bool | None = None
        # The post list's pages from the browser, scrolling to the end;
        # and the stories' page responses (no scrolling: they come with the load).
        self._posts: Responses | None = None
        self._stories: Responses | None = None
        self._started = False
        # The post feed's items waiting for the category that wants them: a playlist's or not.
        self._queues: Dict[str, deque[Dict[str, Any]]] = {"videos": deque(), "playlists": deque()}
        self._told = False

    @property
    def on_page(self) -> bool:
        """Whether the profile's page was opened in the browser."""
        return bool(self._loaded)

    def open(self, *, feeds: bool) -> bool:
        """Open profile on first call, or reload with watchers if `feeds` is needed later."""
        if self._loaded is None:
            if feeds:
                self._posts = watch_responses(self.site, is_feed_of("videos"), scroll=True)
                self._stories = watch_responses(self.site, is_feed_of("stories"))
            self._loaded = self._load()
            return self._loaded
        if feeds and self._posts is None and not self._started:
            return self._watch(posts=True, stories=self._stories is None)
        return self._loaded

    def feed(self, wanted: str) -> Iterator[Dict[str, Any]]:
        """Stream items of `wanted` as the post feed scrolls, or drain queued ones."""
        queue = self._queues[wanted]
        if not self._started:
            yield from self._read_posts(wanted)
        while queue:
            yield queue.popleft()

    def stories(self) -> Iterator[Dict[str, Any]]:
        """The profile's stories from the page's watched responses."""
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

    def tab(self, category: str, tab: str) -> Iterator[Dict[str, Any]]:
        """Open one profile tab in the browser by a click and read its feed to the end."""
        if not self.open(feeds=False):
            return
        css = f'[data-e2e="{tab}"]'
        if not self.site.page.document.css(css):
            self.site.info("{tab}: not public on this account", tab=category)
            return
        for data in self._click_batches(category, css):
            items = items_of(data)
            self.found[category] += len(items)
            yield from items
        if not self.found[category]:
            self.site.info("{tab}: nothing public on this account", tab=category)

    def wait_out_captcha(self) -> None:
        """Until no captcha is left: solved by user or backed off and reloaded."""
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
            self.site.goto(self.url)
            self.site.page.raw.wait_for_timeout(SETTLE * 1000)

    def _click_batches(self, category: str, css: str) -> Iterator[Any]:
        """Click a profile tab and yield its responses live while scrolling to the end."""
        with watch_responses(self.site, is_feed_of(category), scroll=True) as responses:
            try:
                self.site.page.raw.locator(css).first.click(timeout=CLICK_TIMEOUT * 1000)
            # Playwright errors can't be named here; tab that won't open is skipped.
            except Exception as error:
                self.site.warn("{tab}: the tab didn't open: {error}", tab=category,
                               error=str(error).splitlines()[0])
                return
            for data in responses:
                yield data
                if isinstance(data, dict) and "hasMore" in data and not data.get("hasMore"):
                    return

    def _watch(self, *, posts: bool = False, stories: bool = False) -> bool:
        """Start feed watchers and load the profile page."""
        if posts:
            self._posts = watch_responses(self.site, is_feed_of("videos"), scroll=True)
        if stories:
            self._stories = watch_responses(self.site, is_feed_of("stories"))
        self._loaded = self._load()
        return self._loaded

    def _load(self) -> bool:
        """Load the profile, wait out any captcha, and read its own data."""
        if not self.site.goto(self.url):
            raise SiteError("the profile didn't load")
        # The captcha shows a few seconds after the load event, over a page that looks ready.
        self.site.page.raw.wait_for_timeout(SETTLE * 1000)
        self.wait_out_captcha()
        # Reload finding no data keeps earlier data, preserving secUid.
        doc_user = self.site.page.document.script(PAGE_DATA).jmespath(USER).first
        self.user = doc_user or self.user or {}
        return True

    def _read_posts(self, wanted: str) -> Iterator[Dict[str, Any]]:
        """Scroll `videos` feed to its end (or `OLD_STREAK`), yielding `wanted` items live."""
        self._started = True
        if not self.open(feeds=True):
            return
        since = self.site.time.since if wanted == "videos" else None
        old = 0
        try:
            while self._posts is not None:
                ended, old = yield from self._drain_posts(wanted, since, old)
                if ended or self.found["videos"] or not has_captcha(self.site):
                    break
                self._posts = watch_responses(self.site, is_feed_of("videos"), scroll=True)
                self.wait_out_captcha()
        finally:
            self._end()

    def _drain_posts(
        self, wanted: str, since: date | None, old: int,
    ) -> Generator[Dict[str, Any], None, tuple[bool, int]]:
        """Yield `wanted` items per batch until end, `hasMore` false, or `OLD_STREAK`."""
        assert self._posts is not None
        queue = self._queues[wanted]
        for data in self._posts:
            items = items_of(data)
            self.found["videos"] += len(items)
            for item in items:
                self._queues["playlists" if item["playlist"] else "videos"].append(item)
            while queue:
                yield queue.popleft()
            old = older_streak(items, since, old)
            has_more = isinstance(data, dict) and data.get("hasMore")
            no_more = isinstance(data, dict) and "hasMore" in data and not has_more
            if no_more or old >= OLD_STREAK or _hit_saved(items, wanted):
                return True, old
        return False, old

    def _end(self) -> None:
        """Stop watching post feed, reporting total videos found."""
        if self._posts is not None:
            self._posts.close()
            self._posts = None
        if not self._told:
            self._told = True
            listed = selector.parse(self.user).jmespath("stats.videoCount").first
            self.site.info(
                "videos: {n} found; the profile lists {listed}",
                n=self.found["videos"], listed=listed,
            )


class TikTok(Source):
    """TikTok profiles in a browser, and single videos from the embed page."""

    code = "tik"
    name = "TikTok"
    # Short-link hosts (vm./vt.tiktok.com) are subdomains: short link is read by
    # the engine, which follows it once when no route matches.
    domains = ("tiktok.com",)
    needs = ("sessionid", "sid_tt")
    user_agent = TIKTOK_UA
    browser = True
    # No known limit; a captcha doubles the gap.
    pace = 0.2
    # What a post's files are named and keyed by, as they always were: `<date>_<id>` for a video and
    # `<date>_<id>_<n>` for the n-th photo (n from 1), the key the same without the date. `idx` is
    # empty for a video, so the `_` before it goes. A post's `part` is its folder (videos, images,
    # a category, or albums), and its `album` the playlist, under albums.
    folder = "{part}/{album}"
    fields = ("part", "album")
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

    # Link's `@` is in pattern, so regex is explicit; default `{user}` would
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
        """The videos and photo posts that are in no playlist."""
        for item in profile.feed("videos"):
            yield post_of(site, item, "videos")
        if not profile.found["videos"] and profile.on_page:
            # A walled API gave nothing: read each post linked on the page from its embed page.
            links = [match.group(2) for href in site.page.document.css(POST_LINKS).get()
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
        """The profile picture under avatar/avatar_<id>.jpg; keyed by path."""
        if not profile.open(feeds=False):
            return
        url = selector.parse(profile.user).jmespath("user.avatarLarger").first
        if url:
            # The path's last part is "<id>~<size options>.jpeg"; the id names the picture.
            picture_id = PurePosixPath(urlsplit(url).path).name.split("~")[0]
            f = File(url, name=f"avatar_{picture_id}.jpg")
            yield Post(picture_id, [f], headers=cdn_headers(site.cookies))


def embed_posts(site: Site, video_id: str) -> Iterator[Post]:
    """The posts of one video or photo post from public embed endpoint.

    Args:
        site: the target's site; the embed page needs no browser.
        video_id: the post's id.
    """
    try:
        embed_url = f"https://www.tiktok.com/embed/v2/{video_id}"
        page = site.fetch(embed_url).html
    # A failed fetch is a warning for this one post, so the rest of the run goes on.
    except (SiteError, NetworkError) as error:
        site.warn("{err} on embed endpoint for {id}", err=error, id=video_id)
        return
    data = page.script("__NEXT_DATA__").first
    item_struct = (
        selector.parse(data).jmespath("props.pageProps.itemStruct").first
        if data else None
    )
    if not item_struct:
        site.warn("Could not extract item data from embed HTML for {id}", id=video_id)
        return
    for item in items_of({"itemList": [item_struct]}):
        yield post_of(site, item, "videos")


def post_of(site: Site, item: Dict[str, Any], category: str) -> Post:
    """One file of an item as a post."""
    part, album = category, ""
    if category == "videos" and item["playlist"]:
        part, album = "albums", item["playlist"]
    elif category == "videos":
        part = "videos" if item["kind"] == "video" else "images"
    idx = item["idx"] or ""
    values = {"date": day(item["date"]), "id": item["id"], "idx": idx}
    name = fill(TikTok.filename, values) + (".mp4" if item["kind"] == "video" else ".jpg")
    return Post(
        item["id"], [File(item["url"], name=name)], date=item["date"],
        headers=cdn_headers(site.cookies),
                part=part, album=album, idx=idx)


def cdn_headers(cookies: Mapping[str, str]) -> Dict[str, str]:
    """CDN headers (Referer and session cookies) to prevent 403.

    Args:
        cookies: the target's cookies, by name.
    """
    token = cookies.get("tt_chain_token", "")
    sess = cookies.get("sessionid", "")
    # CDN cookie carries tt_chain_token when session has one; sessionid always sent.
    cookie_header = f"tt_chain_token={token}; sessionid={sess}" if token else f"sessionid={sess}"
    return {"Referer": "https://www.tiktok.com/", "User-Agent": TIKTOK_UA, "Cookie": cookie_header}


def items_of(data: Any) -> List[Dict[str, Any]]:
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
    for raw in selector.parse(data).jmespath("itemList || storyList || story_list").get():
        item_id = str(raw.get("id") or raw.get("aweme_id") or "")
        if not item_id:
            continue
        pl_name = selector.parse(raw).jmespath("playlist.name || playlist_info.name").first
        common = {"id": item_id, "date": stamp_of(raw, item_id), "playlist": pl_name}

        # A photo post has `imagePost` and no video; the two are never both read.
        images = selector.parse(raw).jmespath("imagePost.images").get()
        if images:
            # A photo-mode carousel: each picture is a file of its own, numbered from 1.
            for idx, img in enumerate(images):
                path = "imageURL.urlList || displayImage.urlList || thumbnail.urlList"
                url_list = selector.parse(img).jmespath(path).get()
                if url_list:
                    items.append({
                        **common, "url": mirrors(url_list), "kind": "image",
                        "idx": idx + 1,
                    })
        elif video_url := best_video(raw.get("video") or {}):
            items.append({**common, "url": video_url, "kind": "video", "idx": 0})
    return items


def stamp_of(raw: Dict[str, Any], item_id: str) -> Any:
    """Post Unix time from createTime or id's upper 32 bits, or None.

    The engine turns it into a day, in UTC (`day`), as twt and tlg do: a local day would name the
    same video differently, and put it on the other side of `--since`, on two machines.

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
    """Every CDN address for a file as URL or mirror tuple.

        mirrors(["https://v16.example/a", "https://v19.example/a"])     # both, tried in that order

    TikTok answers 403 for some posts on one CDN host and not on another, so every address is kept
    and `flnd._http.download` tries the next one when one refuses.
    """
    found = tuple(dict.fromkeys(str(url) for url in urls if url))
    return found[0] if len(found) == 1 else found


def best_video(video: Dict[str, Any]) -> str | tuple[str, ...] | None:
    """The URL of the video's best file with CDN mirrors: largest MP4 or playAddr."""
    # Each candidate is (pixels, bitrate, url): largest picture wins, bitrate breaks tie.
    candidates = [
        (_pixels(entry), entry.get("Bitrate") or 0, m)
        for entry in video.get("bitrateInfo") or []
        if isinstance(entry, dict) and not _is_dash(entry)
        and (urls := selector.parse(entry).jmespath("PlayAddr.UrlList").get())
        and (m := mirrors(urls))
    ]
    if candidates:
        return max(candidates, key=lambda candidate: candidate[:2])[2]
    address = video.get("playAddr")
    if isinstance(address, dict):
        address = mirrors(address.get("urlList") or [])
    return address or None


def _pixels(entry: Dict[str, Any]) -> int:
    """A `bitrateInfo` entry's width × height, or 0 when TikTok leaves either out."""
    size = entry.get("PlayAddr") or {}
    # Sizes come as numbers, but string or null ranks file last.
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
    marks = (
        str(entry.get("Format") or ""),
        str(entry.get("GearName") or ""),
        str(play.get("UrlKey") or ""),
    )
    urls = play.get("UrlList") or []
    return (any("dash" in mark.lower() for mark in marks)
            or any(urlsplit(str(url)).path.lower().endswith(".mpd") for url in urls))
