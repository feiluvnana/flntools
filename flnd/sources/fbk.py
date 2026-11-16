"""Facebook: a profile's posts, photos, tagged photos, albums, reels, videos, stories and pictures.

Everything is read in a browser logged in with saved cookies. The route loads the profile
page once to extract header metadata (pictures, story buckets, tab paths). Tab categories
read Comet GraphQL results from HTML and scroll to the end with `site.feed` before
yielding media posts.

Media results yield photos (highest resolution) and videos (DASH streams or progressive
mirrors). Albums and stories are read across their respective pages.

Folder layout under `.flnd/media/fbk/<username_or_id>/`:
  posts/, photos/, tagged/, reels/, videos/   <date>_<id>.jpg (or .mp4)
  albums/<album_title>/                       <date>_<photo_id>.jpg
  stories/, profile/                          <date>_<id>.jpg (or .mp4), profile_<id>.jpg
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import chain
from pathlib import PurePosixPath
from typing import Any, Callable, Iterator, NamedTuple
from urllib.parse import parse_qs, urlsplit

from flnd.source import Blocked, File, LoginError, Post, Site, Source, day, target
from flnd.sources.common import dash
from flnd.sources.common.relay import meta_results
from flnd.sources.common.feed import warm_tabs
from flnd import _html

SITE = "https://www.facebook.com"
# What a single post, reel or video is refused with: only a profile is read.
SINGLE_POST = "single posts and videos aren't supported yet; give the profile"
# The CDN refuses a request without the site's Referer.
CDN_HEADERS = {"Referer": f"{SITE}/"}


class Tab(NamedTuple):
    """A profile tab: its category, and the paths it may have, as the profile header names them."""

    # The tab's name, which is its folder and its category, such as "photos".
    category: str
    # The paths Facebook may give the tab on a profile, in the order to try them.
    paths: tuple[str, ...]


# The profile's tabs. The posts tab's path is empty: it is the profile page itself.
TABS = (
    Tab("posts", ("",)),
    Tab("photos", ("photos_by", "photos")),
    Tab("tagged", ("photos_of",)),
    Tab("albums", ("photos_albums",)),
    Tab("reels", ("reels_tab", "reels")),
    Tab("videos", ("videos_by", "videos")),
)
BY_CATEGORY = {tab.category: tab for tab in TABS}
# Where the profile header's user sits in the first page's results.
HEADER = "data.user.profile_header_renderer.user"
# A story page's cards: each is one story, with its media under `attachments`.
STORY_CARDS = "data.bucket.unified_stories_with_notes.edges[].node"
# Where a video's progressive files are: Facebook moved them under its delivery fragment.
PROGRESSIVE = (
    "videoDeliveryResponseFragment.videoDeliveryResponseResult.progressive_urls"
    " || progressive_urls"
)
# Where a video's DASH manifest is, beside its progressive files.
MANIFEST = (
    "videoDeliveryResponseFragment.videoDeliveryResponseResult.dash_manifests[0].manifest_xml"
    " || dash_manifests[0].manifest_xml || dash_manifest"
)
# Video URL fallbacks, best first, for a node with neither DASH nor progressive files.
VIDEO_URLS = (
    "browser_native_hd_url", "playable_url_quality_hd", "browser_native_sd_url", "playable_url",
)
# A photo's image URL: the first of these that the node has, in this order.
PHOTO_URL = "viewer_image.uri || large_share_image.uri || photo_image.uri || image.uri"
# A grid tile is a collection item: its photo is a small copy, which `photo_item` leaves out.
TILES = {"TimelineAppCollectionItem"}
# The profile header's cover and pictures are the profile category's, not any tab's.
NOT_MEDIA = {"profile_header_renderer"}
# Date fields in order of preference; `posted_on` takes the first with a valid stamp.
TIMESTAMPS = ("creation_time", "publish_time", "post_timestamp", "timestamp", "created_time")
# The Comet GraphQL endpoint's responses carry the feed; any other response is not read.
FEED = "graphql"
# A photo page's own day: taken to be the first `created_time` it holds. Unverified until a hand
# check on a live page; a wrong guess only dates a photo that would otherwise be estimated.
CREATED_TIME = re.compile(r'"created_time":(\d{9,11})\b')
# What the page says when Facebook rate-limits a session.
BLOCKED = "temporarily blocked"
# What the countdown says while a block or a login wall is waited out.
TEMPORARILY_BLOCKED = "Facebook says you've been temporarily blocked"
LOGIN_WALL = "Facebook asked for a login (if it keeps asking, run `flnd login fbk` again)"
WALL_ATTEMPTS = 2
BLOCK_ATTEMPTS = 3
# What a GraphQL answer holds instead of data when Facebook blocks it: Comet error page.
COMET_ERROR = "CometErrorRoot.react"
# How deep under a line's `data` a tab's `page_info` sits (data → node → connection → page_info); a
# deeper one is a comment list's or a nested story's, which isn't the tab's next page.
PAGE_INFO_DEPTH = 3


class Header(NamedTuple):
    """Profile header: id, pictures, story buckets, and tab paths."""

    # The profile's id, which keeps other accounts' media out of its tabs.
    user_id: str | None
    # The profile picture's URL and its photo id, when the header has one.
    picture: tuple[str, str] | None
    # The cover photo's URL and its photo id, when the header has one.
    cover: tuple[str, str] | None
    # The story bucket ids, each read from its own page.
    story_buckets: list[str]
    # The tab paths the header lists, from its navigation sections and their collections.
    paths: set[str]


@dataclass
class Profile:
    """A loaded profile page: what every category of the target reads from.

    The page's embedded results and the responses that came while it loaded are the posts tab's
    first batch, so the posts category doesn't load the page again.
    """

    # The profile's URL, without a tab.
    url: str
    # The results the page embeds; None when it didn't load.
    results: list[Any] | None
    # The responses that arrived while it loaded.
    early: list[Any]
    # The profile header, or None when the page had none.
    header: Header | None

    @property
    def owner(self) -> str | None:
        """The profile's own id, which keeps media another account owns out of its tabs.

        None without a header, and then no media is left out.
        """
        return self.header.user_id if self.header else None

    @property
    def paths(self) -> set[str]:
        """The tab paths the header names; empty without a header, when every tab is tried."""
        return self.header.paths if self.header else set()


class Facebook(Source):
    """Facebook profiles, read in a browser that is logged in with the saved cookies."""

    code = "fbk"
    name = "Facebook"
    domains = ("facebook.com", "fb.com", "fb.watch")
    needs = ("c_user", "xs")
    browser = True
    # No published limit: the learned gap doubles on a throttle and is saved, so the first block
    # teaches it; the floor only keeps a burst slower than the other sites'.
    pace = 0.2
    # A file's folder is where it was found: `part` is its tab (or "videos" or "albums"), `album`
    # the album's title when it has one. Each file is named by `post_of`.
    folder = "{part}/{album}"
    fields = ("part", "album")
    filename = "{date}_{id}"
    key = "{id}"
    # Refused before any route. Each page is exact; `*/posts` also refuses `/posts/<id>`.
    unsupported = {
        f"{host}/{path}": SINGLE_POST for host in ("facebook.com", "fb.com")
        for path in (
            "*/posts", "watch", "reel", "reels", "videos", "photo", "photos", "groups",
            "events", "stories", "story.php",
        )
    }

    # `profile.php?id=` and `people/*/<id>` come before the name pattern, skipping both.
    # `@name` has its own pattern.
    def date(self, site: Site, post: Post) -> Any:
        """A photo's day from its own page with created_time."""
        # Asked from the tab, as the page itself does, so it carries the feed's session.
        page = site.fetch(f"{SITE}/photo/?fbid={post.id}", in_page=True).text
        found = CREATED_TIME.search(page)
        return int(found[1]) if found else None

    @target(
        "{user}",
        r"facebook.com/profile.php?id={user:\d+}", r"fb.com/profile.php?id={user:\d+}",
        r"facebook.com/people/*/{user:\d+}", r"fb.com/people/*/{user:\d+}",
        r"facebook.com/@{user}", r"fb.com/@{user}",
        r"facebook.com/{user:(?!profile\.php$|people$|share$)[\w.]+}",
        r"fb.com/{user:(?!profile\.php$|people$|share$)[\w.]+}",
    )
    def user(self, site: Site, user: str) -> Profile:
        """A profile by user name or id: Target("fbk", "user", "example.page")."""
        url = f"{SITE}/{user}"
        # Subscribed before the page loads, so the responses of its first load are kept as well.
        with site.feed(FEED, lines=True) as arrived:
            results = read_page(site, url)
            # Waits for the network to go quiet once, which a page that failed to load doesn't need.
            early = list(arrived) if results is not None else []
        header = next(
            (h for result in results or [] if (h := read_header(result)) is not None), None,
        )
        return Profile(url, results, early, header)

    # Posts tab is profile page itself; must come first to read the already-loaded page
    # route left open, and the tabs after it navigate away.
    @user.category
    def posts(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile's own posts: photos and videos from page and scroll."""
        yield from read_tab(site, profile, BY_CATEGORY["posts"])

    @user.category
    def photos(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The photos tab."""
        yield from read_tab(site, profile, BY_CATEGORY["photos"])

    @user.category
    def tagged(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The photos the profile is tagged in."""
        yield from read_tab(site, profile, BY_CATEGORY["tagged"])

    @user.category
    def albums(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The albums tab, scrolling each album into albums/<title>/."""
        tab = BY_CATEGORY["albums"]
        path = tab_path(tab, profile.paths)
        if path is None:
            site.info("{tab}: this profile has no such tab", tab=tab.category)
            return
        listed: dict[str, str] = {}
        with site.feed(FEED, scroll=True, lines=True, cursor=tab_cursor()) as responses:
            results = read_page(site, f"{profile.url}/{path}")
            if results is None:
                return
            for result in chain(results, responses):
                if isinstance(result, dict):
                    listed.update(album_links(result))
                yield from media_posts(result, tab.category, owner=profile.owner)
        warm_tabs(site, list(listed))
        for url, title in listed.items():
            yield from _read_album(site, url, title=title, owner=profile.owner)

    @user.category
    def reels(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The reels tab."""
        yield from read_tab(site, profile, BY_CATEGORY["reels"])

    @user.category
    def videos(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The videos tab."""
        yield from read_tab(site, profile, BY_CATEGORY["videos"])

    @user.category
    def stories(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The stories of each story bucket listed in the header."""
        if profile.header is None:
            site.warn("the profile header wasn't found; its stories are skipped")
            return
        urls = [f"{SITE}/stories/{bucket}/" for bucket in profile.header.story_buckets]
        warm_tabs(site, urls)
        for url in urls:
            results = read_page(site, url)
            if results is None:
                continue
            cards = [
                card for result in results
                for card in _html.parse(result).jmespath(STORY_CARDS).get()
                if isinstance(card, dict)
            ]
            for card in cards:
                stamp = posted_on(card)
                for media in _html.parse(card).jmespath("attachments[].media").get():
                    item = video_item(media, stamp) or photo_item(media, stamp)
                    if item is not None:
                        yield post_of(item, "stories", headers=CDN_HEADERS)
            site.info("stories: {n} in {url}", n=len(cards), url=url)

    @user.category(folder="profile", key="")
    def profile(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile photo and cover photo, named by photo id."""
        if profile.header is None:
            site.warn("the profile header wasn't found; its pictures are skipped")
            return
        for kind, picture in (("profile", profile.header.picture), ("cover", profile.header.cover)):
            if picture:
                url, photo_id = picture
                yield Post(photo_id, [File(url, name=f"{kind}_{photo_id}.{image_extension(url)}")],
                           headers=CDN_HEADERS)


def read_page(site: Site, url: str) -> list[Any] | None:
    """Load a page and return its embedded results, or None."""
    walls = blocks = 0
    while True:
        # Page results are embedded in HTML; initial load fetches nothing extra.
        results = meta_results(site, url)
        if results:
            return results
        # Where the tab is tells a page that didn't load from a refusal and from an empty page.
        # The first segment: a profile like `/login.example` is no wall. The counts never reset
        # each other, so a wall and a block in turn still give up.
        if urlsplit(site.url).path.split("/")[1:2] in (["login"], ["login.php"]):
            if walls >= WALL_ATTEMPTS:
                raise LoginError("fbk: Facebook kept asking for a login",
                                 hint="run flnd login fbk, then try again")
            walls += 1
            site.back_off(LOGIN_WALL)
            continue
        if BLOCKED in site.document().text.lower():
            if blocks >= BLOCK_ATTEMPTS:
                raise Blocked(TEMPORARILY_BLOCKED)
            blocks += 1
            site.back_off(TEMPORARILY_BLOCKED)
            continue
        # `site.open` leaves the tab where it was on failure; elsewhere means it didn't load.
        return [] if _place(site.url) == _place(url) else None



def _place(url: str) -> tuple[str, str]:
    """A URL's path and query, without a trailing slash: where a tab is, whatever its host."""
    parts = urlsplit(url)
    return parts.path.rstrip("/"), parts.query


def _read_album(site: Site, url: str, *, title: str, owner: str | None) -> Iterator[Post]:
    """Scroll one album page and stream its media posts."""
    with site.feed(FEED, scroll=True, lines=True, cursor=tab_cursor()) as responses:
        results = read_page(site, url)
        if results is None:
            return
        for result in chain(results, responses):
            yield from media_posts(result, "albums", album=title, owner=owner)


def read_tab(site: Site, profile: Profile, tab: Tab) -> Iterator[Post]:
    """The posts of one tab: stream media posts while scrolling to the end."""
    path = tab_path(tab, profile.paths)
    if path is None:
        site.info("{tab}: this profile has no such tab", tab=tab.category)
        return
    upcoming = TABS[TABS.index(tab) + 1:]
    urls = [f"{profile.url}/{p}" for t in upcoming if (p := tab_path(t, profile.paths))]
    warm_tabs(site, urls)
    seen: set[str] = set()
    for result, item_owner in _collect_tab(site, profile, path):
        yield from media_posts(result, tab.category, seen=seen, owner=item_owner)


def _collect_tab(site: Site, profile: Profile, path: str) -> Iterator[tuple[Any, str | None]]:
    """Scroll one tab to its end, yielding `(result, owner)` pairs live."""
    blocks = 0
    with site.feed(FEED, scroll=True, lines=True, cursor=tab_cursor()) as responses:
        if path or profile.results is None:
            results = read_page(site, f"{profile.url}/{path}")
            if results is None:
                return
            first, owner = results, profile.owner
        else:
            first, owner = [*profile.results, *profile.early], None
        for result in first:
            yield result, owner
        for result in responses:
            if not comet_blocked(result):
                blocks = 0
                yield result, profile.owner
                continue
            if blocks >= BLOCK_ATTEMPTS:
                raise Blocked(TEMPORARILY_BLOCKED)
            blocks += 1
            site.back_off(TEMPORARILY_BLOCKED)


def comet_blocked(result: Any) -> bool:
    """Whether a GraphQL result is Facebook's Comet block error."""
    return isinstance(result, dict) and result.get("__dr") == COMET_ERROR


def page_info(line: Any) -> tuple[bool, str | None] | None:
    """Return next page cursor if result line has next page, else None.

    The `page_info` is looked for in `data` at most 3 objects deep, never inside a list, which is
    where the tab's connection keeps it and a comment list's never is.
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
    """A cursor reader for one scrolled tab's feed (`site.feed(cursor=…)`): `page_info`, except
    that a page with no next one ends the feed only after some page said there was one.

    2026-11, live: on some loads the profile's Tiles feed (its intro photos,
    `ProfileCometTilesFeedPaginationQuery`) answers first, with `has_next_page` false, and the
    tab's own pages come after it. That false says nothing about the tab, so it is read as no
    news; the tab's own last page, after its earlier pages, still ends the feed at once.

    Returns:
        A new reader, for one feed: it remembers whether a next page was seen.
    """
    seen_more = False

    def read(line: Any) -> tuple[bool, str | None] | None:
        nonlocal seen_more
        state = page_info(line)
        if state is None:
            return None
        if state[0]:
            seen_more = True
            return state
        return state if seen_more else None

    return read


def media_posts(
    result: Any, tab: str, *, album: str | None = None, owner: str | None = None,
    seen: set[str] | None = None,
) -> Iterator[Post]:
    """Each photo and video of owner in one result of a tab."""
    if not isinstance(result, dict):
        return
    for item in media_items(result, seen=seen, owner=owner):
        if item.album or album:
            part, title = "albums", item.album or album
        elif item.kind == "video" and tab not in ("reels", "videos"):
            part, title = "videos", None
        else:
            part, title = tab, None
        yield post_of(item, part, album=title, headers=CDN_HEADERS)


def post_of(
    item: Item, part: str, *, album: str | None = None, headers: dict[str, str] | None = None,
) -> Post:
    """One item as a post of one file, named `<date>_<id>.<ext>`."""
    first = item.url if isinstance(item.url, str) else item.url[0]
    extension = (
        (item.extension or "mp4").lstrip(".") if item.kind == "video" else image_extension(first)
    )
    date = day(item.date)
    name = f"{date}_{item.id}.{extension}" if date else f"{item.id}.{extension}"
    return Post(
        item.id, [File(item.url, audio=item.audio, name=name)], date=item.date, headers=headers,
        part=part, album=album,
    )


def album_links(data: Any) -> dict[str, str]:
    """The albums a result lists, as {url: title}: each album's tile on the albums tab."""
    tiles = _html.parse(data).jsonpath("$..[?@.__typename == 'TimelineAppCollectionItem']").get()
    return {
        tile["url"]: _html.parse(tile).jmespath("title.text").first or "album"
        for tile in tiles
        if _html.parse(tile).jmespath("node.__typename").first == "Album" and tile.get("url")
    }


def tab_path(tab: Tab, paths: set[str]) -> str | None:
    """The path of a tab on this profile from header or first known fallback."""
    if not tab.paths[0] or not paths:
        return tab.paths[0]
    return next((path for path in tab.paths if path in paths), None)


def read_header(result: Any) -> Header | None:
    """Profile header pictures, story buckets and tab paths, or None."""
    user = _html.parse(result).jmespath(HEADER).first
    if not isinstance(user, dict):
        return None
    found = _html.parse(user)
    picture = found.jmespath("profilePicLarge.uri").first
    picture_id = found.jmespath("profilePhoto.id").first
    cover = found.jmespath("cover_photo.photo.image.uri").first
    cover_id = found.jmespath("cover_photo.photo.id").first
    sections = found.jmespath(
        "profile_tabs.profile_user.timeline_nav_app_sections.edges[].node"
    ).get()
    paths = {section.get("tab_key") for section in sections} | {
        collection.get("tab_key") for section in sections
        for collection in _html.parse(section).jmespath("all_collections.nodes").get()}
    buckets = [
        str(node["id"]) for node in found.jmespath("story_bucket.nodes").get() if node.get("id")
    ]
    return Header(
        user_id=user.get("id"),
        picture=(picture, picture_id or "picture") if picture else None,
        cover=(cover, cover_id or "cover") if cover else None,
        story_buckets=buckets,
        paths={path for path in paths if path},
    )


class Item(NamedTuple):
    """One photo or video found in a result."""

    # The photo's or video's id, which names its file and is its key in the history.
    id: str
    # The file's URL: a video's merged stream, progressive/HD file, or image.
    # video's every address, HD first then SD, as mirrors.
    url: str | tuple[str, ...]
    # "image" or "video"; a video is saved as an mp4.
    kind: str
    # When it was posted, as the site gives it (Unix seconds); the engine reads it as a UTC day.
    date: Any
    # The album's title, when the photo sits in one.
    album: str | None = None
    # A DASH video's audio stream, merged into it.
    audio: str | None = None
    # File extension when not the default mp4 (e.g. webm).
    extension: str | None = None


def media_items(
    data: Any, stamp: Any = None, seen: set[str] | None = None, *,
    owner: str | None = None,
) -> Iterator[Item]:
    """Every photo and video in a result, each once, dated by nearest dated node."""
    seen = set() if seen is None else seen
    if isinstance(data, list):
        for value in data:
            yield from media_items(value, stamp, seen, owner=owner)
        return
    if not isinstance(data, dict):
        return
    # A node's own date wins; a node without one keeps the nearest dated parent's.
    stamp = posted_on(data) or stamp
    item = video_item(data, stamp) or photo_item(data, stamp)
    owned_by = _html.parse(data).jmespath("owner.id || video_owner.id").first
    if item is not None and item.id not in seen and not (owner and owned_by and owned_by != owner):
        seen.add(item.id)
        yield item
    # A video holds its thumbnails and previews as images; they aren't photos of their own.
    if item is not None and item.kind == "video":
        return
    for key, value in data.items():
        if key not in NOT_MEDIA:
            yield from media_items(value, stamp, seen, owner=owner)


def video_item(node: dict, stamp: Any) -> Item | None:
    """A video node's best file: DASH video and audio, HD progressive, or best URL."""
    typename = node.get("__typename", "")
    if not ("Video" in typename or "Reel" in typename or "progressive_urls" in node):
        return None
    video_id = str(node.get("id") or node.get("video_id") or "")
    if video_id and (streams := dash.best(_html.parse(node).jmespath(MANIFEST).first)):
        return Item(
            video_id, streams.video, "video", stamp, audio=streams.audio,
            extension=streams.extension,
        )
    progressive = _html.parse(node).jmespath(PROGRESSIVE).get()
    # The HD file comes first by its quality tag, then the other progressive files, then the node's
    # own URLs (HD before SD): every one a mirror, tried in order when one refuses.
    hd = [
        entry for entry in progressive
        if _html.parse(entry).jmespath("metadata.quality").first == "HD"
    ]
    found = (
        [entry.get("progressive_url") for entry in hd + progressive]
        + [node.get(key) for key in VIDEO_URLS]
    )
    urls = tuple(dict.fromkeys(url for url in found if isinstance(url, str) and url))
    if not video_id or not urls:
        return None
    return Item(video_id, urls[0] if len(urls) == 1 else urls, "video", stamp)


def photo_item(node: dict, stamp: Any) -> Item | None:
    """A photo node's largest image, with its album's name when present."""
    # Tab grid tile holds a small copy of a photo that inner node has in full.
    if node.get("__typename") in TILES:
        return None
    has_image = isinstance(node.get("image"), dict) and "uri" in node["image"]
    if not ("Photo" in node.get("__typename", "") or has_image
            or any(key in node for key in ("large_share_image", "viewer_image", "photo_image"))):
        return None
    photo_id = str(node.get("id") or node.get("photo_id") or node.get("legacy_photo_id") or "")
    url = _html.parse(node).jmespath(PHOTO_URL).first
    if not photo_id or not url:
        return None
    return Item(photo_id, url, "image", stamp, _html.parse(node).jmespath("album.name").first)


def posted_on(node: dict) -> Any:
    """A node's own stamp from the first timestamp field, or None."""
    return next((node[key] for key in TIMESTAMPS if day(node.get(key)) is not None), None)


def image_extension(url: str) -> str:
    """An image's extension from its stp=dst-<format> parameter or path."""
    parts = urlsplit(url)
    # The CDN converts file to format named in `stp=dst-<format>`, matching extension.
    converted = re.search(r"dst-(jpg|png|webp|gif)", parse_qs(parts.query).get("stp", [""])[0])
    if converted:
        return converted[1]
    suffix = PurePosixPath(parts.path).suffix.lower().lstrip(".")
    return suffix if suffix in ("jpg", "png", "webp", "gif") else "jpg"
