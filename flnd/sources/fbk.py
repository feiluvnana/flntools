"""Facebook: a profile's posts, photos, tagged photos, albums, reels, videos, stories and pictures.

Everything is read in a browser logged in with the saved cookies. The profile page comes first:
its header, embedded in the HTML, gives the profile and cover photos, the story buckets, and
which tabs the profile has. Each tab it has is then scrolled to its end, and the Comet GraphQL
results it loads (embedded or fetched) are read for photos (the largest image) and videos (the
DASH manifest's best video, merged with its audio, or else the HD progressive URL), with their real dates. Each story bucket is read from its page.

Folder layout, under `.flnd/downloads/fbk/<username_or_id>/`:
  posts/, photos/, tagged/, reels/, videos/   <YYYY-MM-DD>_<id>.jpg (or .png, .mp4)
  albums/<album_title>/                        <YYYY-MM-DD>_<photo_id>.jpg
  stories/                                     <YYYY-MM-DD>_<id>.jpg (or .mp4)
  profile/                                     profile_<photo id>.jpg, cover_<photo id>.jpg
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Any, Iterator, NamedTuple
from urllib.parse import parse_qs, urlsplit

from flnd.session import Session
from flnd.source import Source, target
from flnd.sources.common import dash
from flnd.sources.common.relay import embedded_results
from shared import fmt, selector

SITE = "https://www.facebook.com"
# What a single post, reel or video is refused with: only a profile is read.
SINGLE_POST = "single posts and videos aren't supported yet; give the profile"


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


class Header(NamedTuple):
    """What the profile header tells: the profile's id, its two pictures, story buckets, and tabs' paths."""

    user_id: str | None
    picture: tuple[str, str] | None
    cover: tuple[str, str] | None
    story_buckets: list[str]
    paths: set[str]


class Facebook(Source):
    """Facebook profiles, read in a browser that is logged in with the saved cookies."""

    code = "fbk"
    name = "Facebook"
    domains = ("facebook.com", "fb.com", "fb.watch")
    needs = ("c_user", "xs")
    categories = (*(tab.category for tab in TABS), "stories", "profile")
    # Refused before any route. Each page is exact, so a profile named `watchman` still reads; `*/posts`
    # also refuses a profile's `/posts/<id>`. A profile's other pages (about, friends) read as the profile.
    unsupported = {f"{host}/{path}": SINGLE_POST for host in ("facebook.com", "fb.com")
                   for path in ("*/posts", "watch", "reel", "reels", "videos", "photo", "photos", "groups",
                                "events", "stories", "story.php")}

    # The routes only read targets: step 6 gives them their bodies, and until then `run` reads the
    # target they give from `session.target`. `profile.php?id=` and `people/*/<id>` come before the
    # name pattern, which skips `profile.php` and `people` so their own forms, or a refusal, decide.
    # `@name` has its own pattern: the name pattern's custom regex takes no `@`, which `{user}` alone would.
    @target("{user}",
            r"facebook.com/profile.php?id={user:\d+}", r"fb.com/profile.php?id={user:\d+}",
            r"facebook.com/people/*/{user:\d+}", r"fb.com/people/*/{user:\d+}",
            r"facebook.com/@{user}", r"fb.com/@{user}",
            r"facebook.com/{user:(?!profile\.php$|people$)[\w.]+}", r"fb.com/{user:(?!profile\.php$|people$)[\w.]+}")
    def user(self, session: Session, user: str) -> None:
        """A profile, by its user name or its id: `Target("fbk", "user", "example.page")`."""

    def run(self, session: Session) -> None:
        profile = f"{SITE}/{session.target.value}"
        feed = Feed()
        session.on_response("graphql", feed.on_result, lines=True)
        session.folder = "posts" if session.wants(category="posts") else None
        results = read_page(session, profile, feed)
        header = next((header for result in results or [] if (header := read_header(result)) is not None), None)
        feed.owner = header.user_id if header else None
        for tab in TABS:
            if not session.wants(category=tab.category):
                continue
            path = tab_path(tab, header.paths if header else set())
            if path is None:
                session.info("{tab}: this profile has no such tab", tab=tab.category)
                continue
            session.folder = tab.category
            if (path or results is None) and read_page(session, f"{profile}/{path}", feed) is None:
                continue
            session.scroll(count=lambda: feed.found[tab.category])
            session.info("{tab}: {files} found", tab=tab.category, files=fmt.count(feed.found[tab.category], "file"))
        self._read_albums(session, feed)
        session.folder = None
        if header is None:
            session.warn("the profile header wasn't found; its pictures and stories are skipped")
            return
        if session.wants(category="profile"):
            save_pictures(session, header)
        if session.wants(category="stories"):
            for bucket in header.story_buckets:
                save_stories(session, f"{SITE}/stories/{bucket}/")

    def _read_albums(self, session: Session, feed: Feed) -> None:
        """Open each album the albums tab listed, and scroll it to its end into albums/<title>/."""
        session.folder = "albums"
        for url, title in list(feed.albums.items()):
            feed.album = title
            if read_page(session, url, feed) is not None:
                session.scroll(count=lambda: feed.found["albums"])
        feed.album = None


@dataclass
class Feed:
    """The files found on each tab so far, the albums listed, the album being read, and the profile's id."""

    found: dict[str, int] = field(default_factory=lambda: {tab.category: 0 for tab in TABS})
    albums: dict[str, str] = field(default_factory=dict)
    album: str | None = None
    owner: str | None = None

    def on_result(self, session: Session, data: Any, response: Any) -> None:
        """Save the files of the tab being read, and note the albums the albums tab lists."""
        # Facebook sends one JSON object per line; other lines were never read.
        tab = session.folder
        if not isinstance(data, dict) or tab not in self.found:
            return
        if tab == "albums":
            self.albums.update(album_links(data))
        self.found[tab] += save_media(session, data, tab, album=self.album, owner=self.owner)


def album_links(data: Any) -> dict[str, str]:
    """The albums a result lists, as {url: title}: each album's tile on the albums tab."""
    tiles = selector.parse(data).jsonpath("$..[?@.__typename == 'TimelineAppCollectionItem']").all
    return {tile["url"]: selector.parse(tile).jmespath("title.text").first or "album"
            for tile in tiles if selector.parse(tile).jmespath("node.__typename").first == "Album" and tile.get("url")}


def save_pictures(session: Session, header: Header) -> None:
    """Save the profile photo and the cover photo, each named by its photo id."""
    for kind, picture in (("profile", header.picture), ("cover", header.cover)):
        if picture:
            url, photo_id = picture
            session.save(url, f"{kind}_{photo_id}.{image_extension(url)}", category="profile", folder="profile",
                         headers={"Referer": f"{SITE}/"})


def read_page(session: Session, url: str, feed: Feed) -> list[Any] | None:
    """Go to a page and read the results its HTML embeds, as the later responses are read; None if it didn't load."""
    if not session.goto(url):
        return None
    results = embedded_results(session.page.document)
    for result in results:
        feed.on_result(session, result, None)
    return results


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


def save_media(session: Session, data: dict, tab: str, *, album: str | None = None, owner: str | None = None) -> int:
    """Save every photo and video of `owner` in one result, into the folder its kind and album give; return how many."""
    count = 0
    for item in media_items(data, owner=owner):
        if item.album or album:
            folder = f"albums/{fmt.filename(item.album or album or '')}"
        elif item.kind == "video" and tab not in ("reels", "videos"):
            folder = "videos"
        else:
            folder = tab
        extension = "mp4" if item.kind == "video" else image_extension(item.url)
        file_name = f"{item.day}_{item.id}.{extension}" if item.day else f"{item.id}.{extension}"
        # The category is the tab it was found on, wherever its folder is, so `--from` picks tagged photos by their tab.
        session.save(item.url, file_name, category=tab, date=item.day, folder=folder, key=item.id,
                     headers={"Referer": f"{SITE}/"}, audio=item.audio)
        count += 1
    return count


def save_stories(session: Session, url: str) -> None:
    """Open one story bucket's page and save each story's photo or video."""
    if not session.goto(url):
        return
    cards = [card for result in embedded_results(session.page.document)
             for card in selector.parse(result).jmespath(STORY_CARDS).all if isinstance(card, dict)]
    for card in cards:
        day = posted_on(card)
        for media in selector.parse(card).jmespath("attachments[].media").all:
            item = video_item(media, day) or photo_item(media, day)
            if item is not None:
                extension = "mp4" if item.kind == "video" else image_extension(item.url)
                file_name = f"{day}_{item.id}.{extension}" if day else f"{item.id}.{extension}"
                session.save(item.url, file_name, category="stories", date=day, folder="stories", key=item.id,
                             audio=item.audio)
    session.info("stories: {n} in {url}", n=len(cards), url=url)


class Item(NamedTuple):
    """One photo or video found in a result."""

    id: str
    url: str
    kind: str
    day: date | None
    album: str | None = None
    # A DASH video's audio stream, merged into it.
    audio: str | None = None


def media_items(data: Any, day: date | None = None, seen: set[str] | None = None, *,
                owner: str | None = None) -> Iterator[Item]:
    """Every photo and video anywhere in a result, each once, with the date of the nearest dated node.

    With `owner`, media another account owns (a shared post, a suggested reel) is left out.
    """
    seen = set() if seen is None else seen
    if isinstance(data, list):
        for value in data:
            yield from media_items(value, day, seen, owner=owner)
        return
    if not isinstance(data, dict):
        return
    day = posted_on(data) or day
    item = video_item(data, day) or photo_item(data, day)
    owned_by = selector.parse(data).jmespath("owner.id || video_owner.id").first
    if item is not None and item.id not in seen and not (owner and owned_by and owned_by != owner):
        seen.add(item.id)
        yield item
    # A video holds its thumbnails and previews as images; they aren't photos of their own.
    if item is not None and item.kind == "video":
        return
    for key, value in data.items():
        if key not in NOT_MEDIA:
            yield from media_items(value, day, seen, owner=owner)


def video_item(node: dict, day: date | None) -> Item | None:
    """A video node's best file: its DASH video and audio, the HD progressive one, or else the best playable URL."""
    typename = node.get("__typename", "")
    if not ("Video" in typename or "Reel" in typename or "progressive_urls" in node):
        return None
    video_id = str(node.get("id") or node.get("video_id") or "")
    if video_id and (streams := dash.best(selector.parse(node).jmespath(MANIFEST).first)):
        return Item(video_id, streams.video, "video", day, audio=streams.audio)
    progressive = selector.parse(node).jmespath(PROGRESSIVE).all
    hd = [entry for entry in progressive if selector.parse(entry).jmespath("metadata.quality").first == "HD"]
    url = next((entry.get("progressive_url") for entry in hd + progressive if entry.get("progressive_url")), None)
    url = url or next((node[key] for key in VIDEO_URLS if node.get(key)), None)
    return Item(video_id, url, "video", day) if video_id and url else None


def photo_item(node: dict, day: date | None) -> Item | None:
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
    return Item(photo_id, url, "image", day, selector.parse(node).jmespath("album.name").first)


def posted_on(node: dict) -> date | None:
    """A node's own day, from the first Unix timestamp it has."""
    for key in TIMESTAMPS:
        stamp = node.get(key)
        if isinstance(stamp, str) and stamp.isdigit():
            stamp = int(stamp)
        if isinstance(stamp, (int, float)) and stamp > 100_000_000:
            return datetime.fromtimestamp(stamp).date()
    return None


def image_extension(url: str) -> str:
    """An image's extension: the format its `stp=dst-<format>` asks the CDN for, or else its path's."""
    parts = urlsplit(url)
    converted = re.search(r"dst-(jpg|png|webp|gif)", parse_qs(parts.query).get("stp", [""])[0])
    if converted:
        return converted[1]
    suffix = PurePosixPath(parts.path).suffix.lower().lstrip(".")
    return suffix if suffix in ("jpg", "png", "webp", "gif") else "jpg"
