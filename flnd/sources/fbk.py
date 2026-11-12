"""Facebook: a profile's posts, photos, tagged photos, albums, reels, videos, stories and pictures.

How it works: everything is read in a browser logged in with the saved cookies.
  1. The route loads the profile page once. Its header, embedded in the HTML (`meta_results`), gives
     the profile and cover photos, the story buckets, and which tabs the profile has.
  2. Each tab category goes to its tab and scrolls it to its end (`watch_responses`). The Comet
     GraphQL results the page embeds and the ones its scrolling loads are read for photos (the
     largest image) and videos (the DASH manifest's best video, merged with its audio, or else the
     HD progressive URL), each with its real date. One media item is one post of one file.
  3. The albums category opens each album its tab lists and scrolls it.
  4. The stories category reads each story bucket from its page.

The site's quirks it works around:
  - the response bodies are newline-delimited JSON, so they are read with `lines=True`;
  - the page's own results come from its HTML, because a first load fetches nothing it needs;
  - a result holds other accounts' media (a shared post, a suggested reel), so the profile's own
    id is read from the header and media of another owner is left out of the scrolled results;
  - a tab's grid tile holds a small copy of a photo that the tile's inner node has in full.

Folder layout, under `.flnd/downloads/fbk/<username_or_id>/`:
  posts/, photos/, tagged/, reels/, videos/   <YYYY-MM-DD>_<id>.jpg (or .png, .mp4)
  albums/<album_title>/                        <YYYY-MM-DD>_<photo_id>.jpg
  stories/                                     <YYYY-MM-DD>_<id>.jpg (or .mp4)
  profile/                                     profile_<photo id>.jpg, cover_<photo id>.jpg
A file is keyed by its id in the history, and a profile picture by its path. Dates are UTC days.

    flnd download fbk:example.page --from photos,albums
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import chain
from pathlib import PurePosixPath
from typing import Any, Iterator, NamedTuple
from urllib.parse import parse_qs, urlsplit

from flnd.source import FetchError, File, LoginError, Post, Site, Source, day, target
from flnd.sources.common import dash
from flnd.sources.common.relay import meta_results
from flnd.sources.common.watch import watch_responses
from shared import fmt, selector

SITE = "https://www.facebook.com"
# What a single post, reel or video is refused with: only a profile is read.
SINGLE_POST = "single posts and videos aren't supported yet; give the profile"
# The CDN refuses a request without the site's Referer.
CDN_HEADERS = {"Referer": f"{SITE}/"}


class Tab(NamedTuple):
    """A profile tab: its category, and the paths it may have, as the profile header names them."""

    category: str
    paths: tuple[str, ...]


TABS = (
    Tab("posts", ("",)),
    Tab("photos", ("photos_by", "photos")),
    Tab("tagged", ("photos_of",)),
    Tab("albums", ("photos_albums",)),
    Tab("reels", ("reels_tab", "reels")),
    Tab("videos", ("videos_by", "videos")),
)
BY_CATEGORY = {tab.category: tab for tab in TABS}
HEADER = "data.user.profile_header_renderer.user"
STORY_CARDS = "data.bucket.unified_stories_with_notes.edges[].node"
# Where a video's progressive files are: Facebook moved them under its delivery fragment.
PROGRESSIVE = "videoDeliveryResponseFragment.videoDeliveryResponseResult.progressive_urls || progressive_urls"
# Where a video's DASH manifest is, beside its progressive files.
MANIFEST = ("videoDeliveryResponseFragment.videoDeliveryResponseResult.dash_manifests[0].manifest_xml"
            " || dash_manifests[0].manifest_xml || dash_manifest")
VIDEO_URLS = ("browser_native_hd_url", "playable_url_quality_hd", "browser_native_sd_url", "playable_url")
PHOTO_URL = "viewer_image.uri || large_share_image.uri || photo_image.uri || image.uri"
TILES = {"TimelineAppCollectionItem"}
# The profile header's cover and pictures are the profile category's, not any tab's.
NOT_MEDIA = {"profile_header_renderer"}
TIMESTAMPS = ("creation_time", "publish_time", "post_timestamp", "timestamp", "created_time")
# The Comet GraphQL endpoint's responses carry the feed; any other response is not read.
FEED = "graphql"
# A Unix stamp below this is a counter, not a date (it would be 1973), so the node has no date.
FIRST_STAMP = 100_000_000
# What the page says when Facebook stops a session that went too fast. Seen 2025; gallery-dl stops on it too.
BLOCKED = "temporarily blocked"


class Header(NamedTuple):
    """What the profile header tells: the profile's id, its two pictures, story buckets, and tabs' paths."""

    user_id: str | None
    picture: tuple[str, str] | None
    cover: tuple[str, str] | None
    story_buckets: list[str]
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
        """The profile's own id, which keeps media another account owns out of its tabs."""
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
    # A file's folder is where it was found: `part` is its tab (or "videos" or "albums"), `album` the
    # album's title when it has one. Each file is named by `post_of`, which spells the extension exactly
    # (the CDN's `stp=dst-<format>`), so `filename` only names a file given without a name.
    folder = "{part}/{album}"
    filename = "{date}_{id}"
    key = "{id}"
    # Refused before any route. Each page is exact, so a profile named `watchman` still reads; `*/posts`
    # also refuses a profile's `/posts/<id>`. A profile's other pages (about, friends) read as the profile.
    unsupported = {f"{host}/{path}": SINGLE_POST for host in ("facebook.com", "fb.com")
                   for path in ("*/posts", "watch", "reel", "reels", "videos", "photo", "photos", "groups",
                                "events", "stories", "story.php")}

    # `profile.php?id=` and `people/*/<id>` come before the name pattern, which skips `profile.php` and
    # `people` so their own forms, or a refusal, decide. `@name` has its own pattern: the name pattern's
    # custom regex takes no `@`, which `{user}` alone would.
    @target("{user}",
            r"facebook.com/profile.php?id={user:\d+}", r"fb.com/profile.php?id={user:\d+}",
            r"facebook.com/people/*/{user:\d+}", r"fb.com/people/*/{user:\d+}",
            r"facebook.com/@{user}", r"fb.com/@{user}",
            r"facebook.com/{user:(?!profile\.php$|people$)[\w.]+}", r"fb.com/{user:(?!profile\.php$|people$)[\w.]+}")
    def user(self, site: Site, user: str) -> Profile:
        """A profile, by its user name or its id: `Target("fbk", "user", "example.page")`.

        Loads the profile page once, and reads its header; every category receives the result.

        Raises:
            FetchError: Facebook says the session is temporarily blocked.
            LoginError: Facebook sent the browser to its login page.
        """
        url = f"{SITE}/{user}"
        # Subscribed before the page loads, so the responses of its first load are kept as well.
        with watch_responses(site, FEED, lines=True) as arrived:
            results = read_page(site, url)
            # Waits for the network to go quiet once, which a page that failed to load doesn't need.
            early = list(arrived) if results is not None else []
        header = next((header for result in results or [] if (header := read_header(result)) is not None), None)
        return Profile(url, results, early, header)

    # The posts tab is the profile page itself, so this category must come first: it reads the page the
    # route left open, and the tabs after it navigate away.
    @user.category
    def posts(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile's own posts: the photos and videos its page and its scrolling load."""
        yield from read_tab(site, profile, BY_CATEGORY["posts"])

    @user.category
    def photos(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The photos tab."""
        yield from read_tab(site, profile, BY_CATEGORY["photos"])

    @user.category
    def tagged(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The photos the profile is tagged in, other people's content, taken without `--from` too."""
        yield from read_tab(site, profile, BY_CATEGORY["tagged"])

    @user.category
    def albums(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The albums tab, then each album it lists, scrolled to its end into `albums/<title>/`."""
        tab = BY_CATEGORY["albums"]
        path = tab_path(tab, profile.paths)
        if path is None:
            site.info("{tab}: this profile has no such tab", tab=tab.category)
            return
        listed: dict[str, str] = {}
        found = 0
        with watch_responses(site, FEED, scroll=True, lines=True) as responses:
            results = read_page(site, f"{profile.url}/{path}")
            if results is None:
                return
            for result in chain(results, responses):
                if isinstance(result, dict):
                    listed.update(album_links(result))
                for post in media_posts(result, tab.category, owner=profile.owner):
                    found += 1
                    yield post
        site.info("{tab}: {files} found", tab=tab.category, files=fmt.count(found, "file"))
        # Each album is opened after the tab is read: its tiles are known once the tab has scrolled to its end.
        for url, title in listed.items():
            with watch_responses(site, FEED, scroll=True, lines=True) as responses:
                results = read_page(site, url)
                # An album that didn't load has nothing to scroll.
                if results is None:
                    continue
                for result in chain(results, responses):
                    yield from media_posts(result, tab.category, album=title, owner=profile.owner)

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
        """The stories of each story bucket the header lists, each read from its own page."""
        if profile.header is None:
            site.warn("the profile header wasn't found; its stories are skipped")
            return
        for bucket in profile.header.story_buckets:
            url = f"{SITE}/stories/{bucket}/"
            results = read_page(site, url)
            if results is None:
                continue
            cards = [card for result in results
                     for card in selector.parse(result).jmespath(STORY_CARDS).all if isinstance(card, dict)]
            for card in cards:
                stamp = posted_on(card)
                for media in selector.parse(card).jmespath("attachments[].media").all:
                    item = video_item(media, stamp) or photo_item(media, stamp)
                    if item is not None:
                        yield post_of(item, "stories")
            site.info("stories: {n} in {url}", n=len(cards), url=url)

    @user.category(folder="profile", key="")
    def profile(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile photo and the cover photo, each named by its photo id; keyed by their paths, as ever."""
        if profile.header is None:
            site.warn("the profile header wasn't found; its pictures are skipped")
            return
        for kind, picture in (("profile", profile.header.picture), ("cover", profile.header.cover)):
            if picture:
                url, photo_id = picture
                yield Post(photo_id, [File(url, name=f"{kind}_{photo_id}.{image_extension(url)}")],
                           headers=CDN_HEADERS)


def read_page(site: Site, url: str) -> list[Any] | None:
    """Load a page and return the results it embeds; None when the page didn't load.

    Args:
        site: the target's site.
        url: the page.

    Returns:
        The results, which are empty for a page that loaded without any (its responses may still
        carry the feed); None when the tab is not on the page, so there is nothing to scroll.

    Raises:
        FetchError: the page says the session is temporarily blocked. A hard stop, never waited out:
            more requests only prolong a block.
        LoginError: Facebook sent the browser to its login page, so the saved login no longer works.
    """
    results = meta_results(site, url)
    if results:
        return results
    # A page with no results is not loaded, or Facebook's refusal, or just a page without any: the tab says which.
    page = site.page
    if "/login" in urlsplit(page.url).path:
        raise LoginError("fbk: Facebook asked for a login; run `flnd login fbk` again")
    if BLOCKED in page.document.text.lower():
        raise FetchError("fbk: Facebook says you've been temporarily blocked; wait a while before downloading again")
    # `Site.goto` keeps the tab where it was when a page fails, so the tab not being on the page means it didn't load.
    return [] if _place(page.url) == _place(url) else None


def _place(url: str) -> tuple[str, str]:
    """A URL's path and query, without a trailing slash: where a tab is, whatever its host."""
    parts = urlsplit(url)
    return parts.path.rstrip("/"), parts.query


def read_tab(site: Site, profile: Profile, tab: Tab) -> Iterator[Post]:
    """The posts of one tab: its page's embedded results, then each batch its scrolling loads.

    Args:
        site: the target's site.
        profile: the loaded profile.
        tab: the tab to read; one the header doesn't name is skipped with a note, and without a header the first
            known path is tried.

    A path-less tab (posts) is the profile page itself: its results are the ones the route already read, which
    are read with no owner filter (the header that names the owner came from them).
    """
    path = tab_path(tab, profile.paths)
    if path is None:
        site.info("{tab}: this profile has no such tab", tab=tab.category)
        return
    found = 0
    with watch_responses(site, FEED, scroll=True, lines=True) as responses:
        if path or profile.results is None:
            results = read_page(site, f"{profile.url}/{path}")
            # A tab that didn't load has nothing to scroll.
            if results is None:
                return
            first, owner = results, profile.owner
        else:
            first, owner = [*profile.results, *profile.early], None
        for result in first:
            for post in media_posts(result, tab.category, owner=owner):
                found += 1
                yield post
        for result in responses:
            for post in media_posts(result, tab.category, owner=profile.owner):
                found += 1
                yield post
    site.info("{tab}: {files} found", tab=tab.category, files=fmt.count(found, "file"))


def media_posts(result: Any, tab: str, *, album: str | None = None, owner: str | None = None) -> Iterator[Post]:
    """Each photo and video of `owner` in one result of a tab, as a post filed where its kind and album put it.

    Args:
        result: one GraphQL result; anything that isn't a JSON object is skipped, as Facebook sends other lines too.
        tab: the category it was found on, which is its folder unless it is a video of another tab or in an album.
        album: the album being read, which every file without an album of its own belongs to.
        owner: the profile's id, or None to keep every account's media.
    """
    if not isinstance(result, dict):
        return
    for item in media_items(result, owner=owner):
        if item.album or album:
            part, title = "albums", item.album or album
        elif item.kind == "video" and tab not in ("reels", "videos"):
            part, title = "videos", None
        else:
            part, title = tab, None
        yield post_of(item, part, album=title, headers=CDN_HEADERS)


def post_of(item: Item, part: str, *, album: str | None = None, headers: dict[str, str] | None = None) -> Post:
    """One item as a post of one file, named `<date>_<id>.<ext>` (`<id>.<ext>` when it has no date)."""
    extension = "mp4" if item.kind == "video" else image_extension(item.url)
    date = day(item.date)
    name = f"{date}_{item.id}.{extension}" if date else f"{item.id}.{extension}"
    return Post(item.id, [File(item.url, audio=item.audio, name=name)], date=item.date, headers=headers,
                part=part, album=album)


def album_links(data: Any) -> dict[str, str]:
    """The albums a result lists, as {url: title}: each album's tile on the albums tab."""
    tiles = selector.parse(data).jsonpath("$..[?@.__typename == 'TimelineAppCollectionItem']").all
    return {tile["url"]: selector.parse(tile).jmespath("title.text").first or "album"
            for tile in tiles if selector.parse(tile).jmespath("node.__typename").first == "Album" and tile.get("url")}


def tab_path(tab: Tab, paths: set[str]) -> str | None:
    """The path of a tab on this profile: the first the header names, or the first known one without a header."""
    if not tab.paths[0] or not paths:
        return tab.paths[0]
    return next((path for path in tab.paths if path in paths), None)


def read_header(result: Any) -> Header | None:
    """The profile header's pictures, story buckets and tab paths, or None when this result isn't it."""
    user = selector.parse(result).jmespath(HEADER).first
    if not isinstance(user, dict):
        return None
    found = selector.parse(user)
    picture = found.jmespath("profilePicLarge.uri").first
    picture_id = found.jmespath("profilePhoto.id").first
    cover = found.jmespath("cover_photo.photo.image.uri").first
    cover_id = found.jmespath("cover_photo.photo.id").first
    sections = found.jmespath("profile_tabs.profile_user.timeline_nav_app_sections.edges[].node").all
    paths = {section.get("tab_key") for section in sections} | {
        collection.get("tab_key") for section in sections
        for collection in selector.parse(section).jmespath("all_collections.nodes").all}
    return Header(user_id=user.get("id"), picture=(picture, picture_id or "picture") if picture else None,
                  cover=(cover, cover_id or "cover") if cover else None,
                  story_buckets=[str(node["id"]) for node in found.jmespath("story_bucket.nodes").all if node.get("id")],
                  paths={path for path in paths if path})


class Item(NamedTuple):
    """One photo or video found in a result."""

    id: str
    url: str
    kind: str
    # When it was posted, as the site gives it (Unix seconds); the engine reads it as a UTC day.
    date: Any
    album: str | None = None
    # A DASH video's audio stream, merged into it.
    audio: str | None = None


def media_items(data: Any, stamp: Any = None, seen: set[str] | None = None, *,
                owner: str | None = None) -> Iterator[Item]:
    """Every photo and video anywhere in a result, each once, with the date of the nearest dated node.

    With `owner`, media another account owns (a shared post, a suggested reel) is left out.
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
    owned_by = selector.parse(data).jmespath("owner.id || video_owner.id").first
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
    """A video node's best file: its DASH video and audio, the HD progressive one, or else the best playable URL."""
    typename = node.get("__typename", "")
    if not ("Video" in typename or "Reel" in typename or "progressive_urls" in node):
        return None
    video_id = str(node.get("id") or node.get("video_id") or "")
    if video_id and (streams := dash.best(selector.parse(node).jmespath(MANIFEST).first)):
        return Item(video_id, streams.video, "video", stamp, audio=streams.audio)
    progressive = selector.parse(node).jmespath(PROGRESSIVE).all
    hd = [entry for entry in progressive if selector.parse(entry).jmespath("metadata.quality").first == "HD"]
    url = next((entry.get("progressive_url") for entry in hd + progressive if entry.get("progressive_url")), None)
    url = url or next((node[key] for key in VIDEO_URLS if node.get(key)), None)
    return Item(video_id, url, "video", stamp) if video_id and url else None


def photo_item(node: dict, stamp: Any) -> Item | None:
    """A photo node's largest image, with its album's name when it has one."""
    # A tab's grid tile holds a small copy of a photo that the tile's inner node, or a post, has in full.
    if node.get("__typename") in TILES:
        return None
    has_image = isinstance(node.get("image"), dict) and "uri" in node["image"]
    if not ("Photo" in node.get("__typename", "") or has_image
            or any(key in node for key in ("large_share_image", "viewer_image", "photo_image"))):
        return None
    photo_id = str(node.get("id") or node.get("photo_id") or node.get("legacy_photo_id") or "")
    url = selector.parse(node).jmespath(PHOTO_URL).first
    if not photo_id or not url:
        return None
    return Item(photo_id, url, "image", stamp, selector.parse(node).jmespath("album.name").first)


def posted_on(node: dict) -> Any:
    """A node's own Unix stamp, from the first of its timestamp fields that holds one; None when it has none."""
    for key in TIMESTAMPS:
        stamp = node.get(key)
        if isinstance(stamp, str) and stamp.isdigit():
            stamp = int(stamp)
        if isinstance(stamp, (int, float)) and stamp > FIRST_STAMP:
            return stamp
    return None


def image_extension(url: str) -> str:
    """An image's extension: the format its `stp=dst-<format>` asks the CDN for, or else its path's."""
    parts = urlsplit(url)
    converted = re.search(r"dst-(jpg|png|webp|gif)", parse_qs(parts.query).get("stp", [""])[0])
    if converted:
        return converted[1]
    suffix = PurePosixPath(parts.path).suffix.lower().lstrip(".")
    return suffix if suffix in ("jpg", "png", "webp", "gif") else "jpg"
