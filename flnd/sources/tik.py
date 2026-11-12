"""TikTok: a profile's videos, photo posts, stories, playlists, reposts, liked and favorites, and its picture.

How it works: a profile is opened in a browser, and its lists are read from the JSON responses the
page receives while it scrolls (`watch_responses`); there is no API of the source's own to call.
The route `user` returns a `Profile`, which opens the page the first time a category needs it and
shares what it read between the categories. One video, by id or URL, is read from the public embed
page, without a browser.

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

Folder layout, under `.flnd/downloads/tik/<username>/`:
  videos/<YYYY-MM-DD>_<video_id>.mp4
  images/<YYYY-MM-DD>_<post_id>_<idx>.jpg
  stories/<YYYY-MM-DD>_<story_id>.mp4 (or .jpg)
  albums/<playlist_title>/<YYYY-MM-DD>_<video_id>.mp4
  reposts/, liked/, favorites/   <YYYY-MM-DD>_<video_id>.mp4 (or _<idx>.jpg)
  profile/avatar_<id>.jpg
A single video's folder, `.flnd/downloads/tik/<video_id>/`, has `videos/` or `images/` in it.
"""

from __future__ import annotations

import re
from collections import deque
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, Iterator, List, Mapping, NamedTuple
from urllib.parse import urlsplit

from flnd.source import FetchError, File, Post, Site, Source, day, fill, target
from flnd.sources.common.watch import Responses, watch_responses
from shared import selector

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
# A Unix time before 1973 is a placeholder (0, a small number), not a post's time: the post is then
# dated from its id instead.
FIRST_UNIX_TIME = 100000000


class Feed(NamedTuple):
    """One list the profile loads: its category, and the API path its pages come from."""

    category: str
    api: str


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
    """Whether the page shows a captcha: its container, or its words in the text a reader sees."""
    page = site.page.document
    return bool(page.css(CAPTCHA_CSS)) or bool(CAPTCHA_TEXT.search(" ".join(page.xpath(VISIBLE_TEXT).all)))


class Profile:
    """What a profile's categories share: its page, opened once, and the lists read from it.

        profile = Profile(site, "example_creator")
        profile.open(feeds=True)                  # loads the page, waits out a captcha
        for item in profile.feed("videos"): ...   # reads the page to the end of the videos, once for all

    Args:
        site: the target's site, whose tab is the profile's.
        name: the user name, without `@`.

    The route builds it and opens nothing: `--from profile` needs the page but no scrolling, and
    the response watchers must exist before the page loads or the first responses are lost, so
    the first category that needs the page opens it, as it needs it.
    """

    def __init__(self, site: Site, name: str):
        self.site = site
        self.url = f"https://www.tiktok.com/@{name}"
        # The profile's own data from the page (the picture, the video count); empty until it is open.
        self.user: Dict[str, Any] = {}
        # How many items each feed gave, saved or not, so a second run still sees a list grow.
        self.found = {feed.category: 0 for feed in FEEDS}
        self._loaded: bool | None = None
        # The post feed's pages, scrolling for more, and the stories' (no scrolling: they come with the load).
        self._posts: Responses | None = None
        self._stories: Responses | None = None
        # The post feed's items waiting for the category that wants them: a playlist's or not.
        self._queues: Dict[str, deque[Dict[str, Any]]] = {"videos": deque(), "playlists": deque()}
        self._told = False

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

    def feed(self, wanted: str) -> Iterator[Dict[str, Any]]:
        """The post feed's items of `wanted` ("videos", or "playlists" for those in a playlist), reading the page as needed.

        Scrolling reads both kinds at once; the kind not asked for waits in its queue for the
        category that is. The iterator ends when the feed does.
        """
        queue = self._queues[wanted]
        while True:
            if queue:
                yield queue.popleft()
            elif not self._read():
                return

    def stories(self) -> Iterator[Dict[str, Any]]:
        """The story feeds' items, from what arrived with the page and while it scrolled."""
        stories, self._stories = self._stories, None
        for data in stories or ():
            items = items_of(data)
            self.found["stories"] += len(items)
            yield from items

    def tab(self, category: str, tab: str) -> Iterator[Dict[str, Any]]:
        """Open one profile tab and read its feed to the end; one line when the account doesn't show it.

        Args:
            category: the tab's feed: "reposts", "favorites" or "liked".
            tab: the tab's `data-e2e` name.
        """
        css = f'[data-e2e="{tab}"]'
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

    def wait_out_captcha(self) -> None:
        """Until no captcha is left: a person solves it in the window, or the source backs off and reloads."""
        while has_captcha(self.site):
            if self.site.handoff(until=lambda: not has_captcha(self.site), timeout=CAPTCHA_TIMEOUT,
                                 message="Solve the TikTok captcha in the browser window"):
                return
            self.site.back_off("TikTok captcha")
            self.site.goto(self.url)
            self.site.page.raw.wait_for_timeout(SETTLE * 1000)

    def _load(self) -> bool:
        """Load the profile, wait out any captcha it shows and read its own data; False when it didn't load."""
        if not self.site.goto(self.url):
            return False
        # The captcha shows a few seconds after the load event, over a page that looks ready.
        self.site.page.raw.wait_for_timeout(SETTLE * 1000)
        self.wait_out_captcha()
        self.user = self.site.page.document.script(PAGE_DATA).jmespath(USER).first or {}
        return True

    def _read(self) -> bool:
        """Read the post feed's next response into the queues; False once the feed has ended."""
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

    def _file(self, data: Any) -> None:
        """Queue one post-feed response's items, and end the feed when it says it has no more."""
        items = items_of(data)
        self.found["videos"] += len(items)
        for item in items:
            self._queues["playlists" if item["playlist"] else "videos"].append(item)
        if isinstance(data, dict) and "hasMore" in data and not data["hasMore"] and self._posts is not None:
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
        """One video or photo post, by its id: `Target("tik", "video", "7301234567890123456")`."""
        yield from embed_posts(site, id)

    # The link's `@` is written in the pattern, so its regex is spelled out: a default `{user}` would
    # drop a second `@`, and `tiktok.com/@@name` is refused as it always was.
    @target("{user}", r"tiktok.com/@{user:[\w.]+}")
    def user(self, site: Site, user: str) -> Profile:
        """A profile, by its user name; a `@` in front of it is dropped. Nothing is opened yet."""
        return Profile(site, user)

    # Newest first, apart from pinned videos at the top, which the engine's early stop allows for.
    @user.category(newest_first=True)
    def videos(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The videos and photo posts that are in no playlist; the page's links when the API is walled."""
        if not profile.open(feeds=True):
            return
        for item in profile.feed("videos"):
            yield post_of(site, item, "videos")
        if not profile.found["videos"]:
            # A walled API gave nothing: read each post linked on the page from its embed page.
            links = [match.group(2) for href in site.page.document.css(POST_LINKS).all
                     if (match := re.search(r"/(video|photo)/(\d+)", href))]
            for video_id in dict.fromkeys(links):
                yield from embed_posts(site, video_id)

    @user.category
    def stories(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile's stories, which come with the page."""
        if profile.open(feeds=True):
            for item in profile.stories():
                yield post_of(site, item, "stories")

    @user.category(newest_first=True)
    def playlists(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The videos that are in a playlist, each under `albums/<playlist title>/`."""
        if profile.open(feeds=True):
            for item in profile.feed("playlists"):
                yield post_of(site, item, "videos")

    @user.category
    def reposts(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The Reposts tab. Not `newest_first`: it is ordered by when each was reposted, not posted."""
        if profile.open(feeds=False):
            for item in profile.tab("reposts", "repost-tab"):
                yield post_of(site, item, "reposts")

    @user.category
    def liked(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The Liked tab, when the account made it public."""
        if profile.open(feeds=False):
            for item in profile.tab("liked", "liked-tab"):
                yield post_of(site, item, "liked")

    @user.category
    def favorites(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The Favorites tab, when the account made it public."""
        if profile.open(feeds=False):
            for item in profile.tab("favorites", "favorites-tab"):
                yield post_of(site, item, "favorites")

    @user.category(folder="profile", key="")
    def profile(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile picture, named `avatar_<picture id>.jpg` and keyed by that path, as always."""
        if not profile.open(feeds=False):
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
    try:
        page = site.fetch(f"https://www.tiktok.com/embed/v2/{video_id}", timeout=20.0, follow_redirects=False).html
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
    """What TikTok's CDN wants to not answer 403 Forbidden: the Referer and the session cookies (tt_chain_token)."""
    token = cookies.get("tt_chain_token", "")
    sess = cookies.get("sessionid", "")
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
    for raw in selector.parse(data).jmespath("itemList || storyList || story_list").all:
        item_id = str(raw.get("id") or raw.get("aweme_id") or "")
        if not item_id:
            continue
        common = {"id": item_id, "date": stamp_of(raw, item_id),
                  "playlist": selector.parse(raw).jmespath("playlist.name || playlist_info.name").first}

        images = selector.parse(raw).jmespath("imagePost.images").all
        if images:
            # A photo-mode carousel: each picture is a file of its own, numbered from 1.
            for idx, img in enumerate(images):
                url_list = selector.parse(img).jmespath("imageURL.urlList || displayImage.urlList || thumbnail.urlList").all
                if url_list:
                    items.append({**common, "url": url_list[0], "kind": "image", "idx": idx + 1})
        elif video_url := best_video(raw.get("video") or {}):
            items.append({**common, "url": video_url, "kind": "video", "idx": 0})
    return items


def stamp_of(raw: Dict[str, Any], item_id: str) -> int | float | None:
    """The post's Unix time from createTime, or else from the id's upper 32 bits; None when neither is one.

    The engine turns it into a day, in UTC (`day`), as twt and tlg do: a local day would name the
    same video differently, and put it on the other side of `--since`, on two machines (fix #15, 2026-11).
    """
    create_time = raw.get("createTime") or raw.get("create_time")
    if isinstance(create_time, str) and create_time.isdigit():
        create_time = int(create_time)
    if isinstance(create_time, (int, float)) and create_time > FIRST_UNIX_TIME:
        return create_time
    # An id is a Snowflake: the Unix time in seconds is above the lowest 32 bits.
    stamp = int(item_id) >> 32 if item_id.isdigit() else 0
    return stamp if stamp > FIRST_UNIX_TIME else None


def best_video(video: Dict[str, Any]) -> str | None:
    """The URL of the video's best file: the largest MP4 of `bitrateInfo`, else `playAddr`.

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
    candidates = [(_pixels(entry), entry.get("Bitrate") or 0, urls[0]) for entry in video.get("bitrateInfo") or []
                  if isinstance(entry, dict) and not _is_dash(entry)
                  and (urls := selector.parse(entry).jmespath("PlayAddr.UrlList").all)]
    if candidates:
        return max(candidates, key=lambda candidate: candidate[:2])[2]
    address = video.get("playAddr")
    if isinstance(address, dict):
        address = (address.get("urlList") or [None])[0]
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
