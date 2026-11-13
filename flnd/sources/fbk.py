"""Facebook: a profile's posts, photos, tagged photos, albums, reels, videos, stories and pictures.

How it works: everything is read in a browser logged in with the saved cookies.
  1. The route loads the profile page once. Its header, embedded in the HTML (`meta_results`), gives
     the profile and cover photos, the story buckets, and which tabs the profile has.
  2. Each tab category goes to its tab, reads the Comet GraphQL results the page embeds, and
     scrolls only until the page sends its own next-page `/api/graphql/` POST. That request (its
     `doc_id`, `fb_dtsg`, `lsd` and `variables`) is kept and sent again from the tab with each
     new `cursor` (`Replay`), one small request a page. A replay Facebook refuses (not a block)
     warns once, and the tab is scrolled to its end instead, as before. Results are read for
     photos (the largest image) and videos (the DASH manifest's best video, merged with its audio,
     or else the HD progressive URL), each with its real date. One media item is one post of one file.
  3. The albums category opens each album its tab lists and scrolls it.
  4. The stories category reads each story bucket from its page: there is one per profile, and its
     page holds them all.

A block is seen and waited out (`site.back_off`), never mistaken for the end of a tab: a page that
says "temporarily blocked", a GraphQL answer that is Comet's error page (`CometErrorRoot.react`),
and a page or answer sent to `/login`. The request is then made again, as long as it takes.

The site's quirks it works around:
  - the response bodies are newline-delimited JSON, so they are read with `lines=True`;
  - the page's own results come from its HTML, because a first load fetches nothing it needs;
  - a result holds other accounts' media (a shared post, a suggested reel), so the profile's own
    id is read from the header and media of another owner is left out of the scrolled results;
  - a tab's grid tile holds a small copy of a photo that the tile's inner node has in full.

Folder layout, under `.flnd/media/fbk/<username_or_id>/`:
  posts/, photos/, tagged/, reels/, videos/   <YYYY-MM-DD>_<id>.jpg (or .png, .mp4)
  albums/<album_title>/                        <YYYY-MM-DD>_<photo_id>.jpg
  stories/                                     <YYYY-MM-DD>_<id>.jpg (or .mp4)
  profile/                                     profile_<photo id>.jpg, cover_<photo id>.jpg
A file is keyed by its id in the history, and a profile picture by its path. Dates are UTC days.

    flnd download fbk:example.page --from photos,albums
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from itertools import chain
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Generator, Iterator, NamedTuple
from urllib.parse import parse_qs, urlsplit

from flnd.source import FetchError, File, Post, Site, Source, day, pages, target
from flnd.sources.common import dash
from flnd.sources.common.relay import meta_results
from flnd.sources.common.replay import Replay
from flnd.sources.common.watch import watch_responses
from shared import fmt, selector

if TYPE_CHECKING:
    from shared import http

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
PROGRESSIVE = "videoDeliveryResponseFragment.videoDeliveryResponseResult.progressive_urls || progressive_urls"
# Where a video's DASH manifest is, beside its progressive files.
MANIFEST = ("videoDeliveryResponseFragment.videoDeliveryResponseResult.dash_manifests[0].manifest_xml"
            " || dash_manifests[0].manifest_xml || dash_manifest")
# A video's URL fallbacks, best first, for a node with neither a DASH manifest nor progressive files.
VIDEO_URLS = ("browser_native_hd_url", "playable_url_quality_hd", "browser_native_sd_url", "playable_url")
# A photo's image URL: the first of these that the node has, in this order.
PHOTO_URL = "viewer_image.uri || large_share_image.uri || photo_image.uri || image.uri"
# A grid tile is a collection item: its photo is a small copy, which `photo_item` leaves out.
TILES = {"TimelineAppCollectionItem"}
# The profile header's cover and pictures are the profile category's, not any tab's.
NOT_MEDIA = {"profile_header_renderer"}
# The date fields a node may carry, in the order they are preferred; `posted_on` takes the first that holds a stamp.
TIMESTAMPS = ("creation_time", "publish_time", "post_timestamp", "timestamp", "created_time")
# The Comet GraphQL endpoint's responses carry the feed; any other response is not read.
FEED = "graphql"
# What the page says when Facebook stops a session that went too fast. Seen 2025; gallery-dl stops on it too.
BLOCKED = "temporarily blocked"
# What the countdown says while a block or a login wall is waited out.
TEMPORARILY_BLOCKED = "Facebook says you've been temporarily blocked"
LOGIN_WALL = "Facebook asked for a login (if it keeps asking, run `flnd login fbk` again)"
# What a GraphQL answer holds instead of data when Facebook blocks it: Comet's error page (gallery-dl's sign).
COMET_ERROR = "CometErrorRoot.react"
# The variable a profile tab's pagination query takes its cursor in.
CURSOR = "cursor"
# Facebook's anti-JSON-hijacking prefix, which some answers start with.
HIJACK_PREFIX = "for (;;);"
# How deep under a line's `data` a tab's `page_info` sits (data → node → connection → page_info); a
# deeper one is a comment list's or a nested story's, which isn't the tab's next page.
PAGE_INFO_DEPTH = 3


class Header(NamedTuple):
    """What the profile header tells: the profile's id, its two pictures, story buckets, and tabs' paths."""

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
    # No known limit, and its block is the harshest (plan/phase-6/pace.md), so it starts slower and learns.
    pace = 1.5
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

        Loads the profile page once, and reads its header; every category receives the result. A
        block or a login wall on the way is waited out (`read_page`).

        Args:
            site: the target's site.
            user: the user name, or the numeric id the `profile.php?id=` URL gives.
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
        """The profile's own posts: the photos and videos its page and its scrolling load.

            collect(Facebook, "fbk:example.page", options=Options(categories=("posts",)))

        Args:
            site: the target's site.
            profile: the loaded `Profile` the route made, which every category reads from.
        """
        yield from read_tab(site, profile, BY_CATEGORY["posts"])

    @user.category
    def photos(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The photos tab.

            collect(Facebook, "fbk:example.page", options=Options(categories=("photos",)))

        Args:
            site: the target's site.
            profile: the loaded `Profile` the route made, which every category reads from.
        """
        yield from read_tab(site, profile, BY_CATEGORY["photos"])

    @user.category
    def tagged(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The photos the profile is tagged in, other people's content, taken without `--from` too.

            collect(Facebook, "fbk:example.page", options=Options(categories=("tagged",)))

        Args:
            site: the target's site.
            profile: the loaded `Profile` the route made, which every category reads from.
        """
        yield from read_tab(site, profile, BY_CATEGORY["tagged"])

    @user.category
    def albums(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The albums tab, then each album it lists, scrolled to its end into `albums/<title>/`.

            collect(Facebook, "fbk:example.page", options=Options(categories=("albums",)))

        Args:
            site: the target's site.
            profile: the loaded `Profile` the route made, which every category reads from.
        """
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
        """The reels tab.

            collect(Facebook, "fbk:example.page", options=Options(categories=("reels",)))

        Args:
            site: the target's site.
            profile: the loaded `Profile` the route made, which every category reads from.
        """
        yield from read_tab(site, profile, BY_CATEGORY["reels"])

    @user.category
    def videos(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The videos tab.

            collect(Facebook, "fbk:example.page", options=Options(categories=("videos",)))

        Args:
            site: the target's site.
            profile: the loaded `Profile` the route made, which every category reads from.
        """
        yield from read_tab(site, profile, BY_CATEGORY["videos"])

    @user.category
    def stories(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The stories of each story bucket the header lists, each read from its own page.

            collect(Facebook, "fbk:example.page", options=Options(categories=("stories",)))

        Args:
            site: the target's site.
            profile: the loaded `Profile` the route made, which every category reads from.
        """
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
        """The profile photo and the cover photo, each named by its photo id; keyed by their paths, as ever.

            collect(Facebook, "fbk:example.page", options=Options(categories=("profile",)))

        Args:
            site: the target's site.
            profile: the loaded `Profile` the route made, which every category reads from.
        """
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

    A page that says the session is temporarily blocked, or that Facebook sent to its login page,
    is waited out with `site.back_off` and loaded again, as long as it takes (the user decided on
    2026-11-12 that flnd waits out every block and challenge). A login that really expired keeps
    the countdown going, which says to log in again.
    """
    while True:
        # The page's own results are in its HTML: a first load fetches nothing it needs, so they are read from there.
        results = meta_results(site, url)
        if results:
            return results
        # A page with no results is not loaded, or Facebook's refusal, or just a page without any: the tab says which.
        page = site.page
        if "/login" in urlsplit(page.url).path:
            site.back_off(LOGIN_WALL)
            continue
        if BLOCKED in page.document.text.lower():
            site.back_off(TEMPORARILY_BLOCKED)
            continue
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

    The tab is scrolled until the page sends its own next-page request, which is then replayed with
    each cursor. A tab that has no further page, or whose requests can't be replayed, is scrolled to
    its end; so is one whose replay is refused, after one warning.
    """
    path = tab_path(tab, profile.paths)
    if path is None:
        site.info("{tab}: this profile has no such tab", tab=tab.category)
        return
    found = [0]

    def posts(result: Any, owner: str | None) -> Iterator[Post]:
        for post in media_posts(result, tab.category, owner=owner):
            found[0] += 1
            yield post

    replay: Replay | None = None
    cursor: str | None = None
    with watch_responses(site, FEED, scroll=True, lines=True) as responses:
        # 1. The tab's own page is loaded; the posts tab reads what the route already read, with no owner filter.
        if path or profile.results is None:
            results = read_page(site, f"{profile.url}/{path}")
            # A tab that didn't load has nothing to scroll.
            if results is None:
                return
            first, owner = results, profile.owner
        else:
            first, owner = [*profile.results, *profile.early], None
        for result in first:
            yield from posts(result, owner)
        # 2. Scrolling: each response is read, and the first with a next page is the one replayed.
        for result in responses:
            yield from posts(result, profile.owner)
            captured = Replay.of(responses.request, cursor=CURSOR)
            state = page_info(result)
            if captured is None or state is None:
                continue
            # The other lines of this response are here already; one of them may hold the next page.
            for rest in responses.arrived():
                yield from posts(rest, profile.owner)
                state = page_info(rest) or state
            # A response whose connection has no next page isn't known to be the tab's own feed: the
            # profile's Tiles feed (`ProfileCometTilesFeedPaginationQuery`, its intro photos) answers
            # first on some loads, with `has_next_page` false, which is not the end of the posts. Keep
            # scrolling; the real feed's page, or the end of the scroll, comes after it.
            if state[0]:
                replay, cursor = captured, state[1]
                break
    # 3. The rest of the pages, by replaying the tab's own request with each cursor.
    if replay is not None and cursor:
        refused = yield from replayed(site, replay, cursor, lambda line: posts(line, profile.owner))
        if refused:
            # The tab is still where the scroll left it: scrolling on reads the rest; a repeat has the same key.
            site.warn("{tab}: the page's API refused a replay; scrolling instead", tab=tab.category)
            with watch_responses(site, FEED, scroll=True, lines=True) as responses:
                for result in responses:
                    yield from posts(result, profile.owner)
    site.info("{tab}: {files} found", tab=tab.category, files=fmt.count(found[0], "file"))


def replayed(site: Site, replay: Replay, cursor: str,
             posts: Callable[[Any], Iterator[Post]]) -> Generator[Post, None, bool]:
    """A tab's pages from `cursor` on, by replay; the generator's value is True when a replay was refused.

    Args:
        site: the target's site.
        replay: the tab's own next-page request.
        cursor: the first page's cursor.
        posts: gives the posts of one result line.

    A block is waited out and the same page asked again (`replay_lines`); a refusal, or an answer
    without a next-page marker, is a refusal, and the caller scrolls instead.
    """
    try:
        for lines in pages(lambda following: replay_lines(site, replay, following or cursor), next_cursor):
            for line in lines:
                yield from posts(line)
    # A refused replay is the caller's to scroll past, so it is returned, not raised; a block never gets here (`replay_lines` waits it out).
    except FetchError:
        return True
    return False


def replay_lines(site: Site, replay: Replay, cursor: str) -> list[Any]:
    """One page of a tab by replay, as its JSON lines; a block or a login wall waited out and the page asked again.

    Args:
        site: the target's site.
        replay: the tab's own next-page request.
        cursor: the cursor of the page to ask for.

    Raises:
        FetchError: the replay was refused, or its answer holds no `page_info`, so it isn't a page of the tab.
    """
    while True:
        try:
            response = replay.send(site, cursor)
        # A refused call may still carry Facebook's block page in its body: that is waited out, anything else is raised.
        except FetchError as error:
            if error.response is not None and blocked(error.response):
                site.back_off(TEMPORARILY_BLOCKED)
                continue
            raise
        if "/login" in urlsplit(response.url).path:
            site.back_off(LOGIN_WALL)
            continue
        if blocked(response):
            site.back_off(TEMPORARILY_BLOCKED)
            continue
        found = json_lines(response.text)
        if not any(page_info(line) is not None for line in found):
            raise FetchError("its answer holds no next page")
        return found


def blocked(response: http.Response) -> bool:
    """Whether an answer is Facebook's block: Comet's error page, or its "temporarily blocked" words."""
    text = response.text
    return COMET_ERROR in text or BLOCKED in text.lower()


def json_lines(text: str) -> list[Any]:
    """An answer's newline-delimited JSON, one value a line; a line that isn't JSON is left out."""
    found = []
    for line in text.removeprefix(HIJACK_PREFIX).splitlines():
        if not line.strip():
            continue
        try:
            found.append(json.loads(line))
        # Facebook mixes in lines that aren't JSON now and then; the rest of the answer still counts.
        except ValueError:
            continue
    return found


def page_info(line: Any) -> tuple[bool, str | None] | None:
    """Whether a result line says the tab has a next page, and its cursor; None when the line says nothing.

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


def next_cursor(lines: list[Any]) -> str | None:
    """The cursor of the page after these lines; None when it is the last."""
    for line in lines:
        state = page_info(line)
        if state is not None:
            return state[1] if state[0] else None
    return None


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
    """One item as a post of one file, named `<date>_<id>.<ext>` (`<id>.<ext>` when it has no date).

    Args:
        item: the photo or video.
        part: the folder it is saved in, such as "photos" or "albums".
        album: the album's title, which makes it a file of `albums/<title>/`; None for none.
        headers: the request headers its file is fetched with; None for none.
    """
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
    """The path of a tab on this profile: the first the header names, or the first known one without a header.

        tab_path(BY_CATEGORY["photos"], {"photos_by"})

    Args:
        tab: the tab to find.
        paths: the tab paths the header names; empty without a header.

    Returns:
        The path, which is "" for the posts tab; None when the header names none of the tab's paths.
    """
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

    # The photo's or video's id, which names its file and is its key in the history.
    id: str
    # The file's URL: a video's merged stream, its progressive or HD file, or the image.
    url: str
    # "image" or "video"; a video is saved as an mp4.
    kind: str
    # When it was posted, as the site gives it (Unix seconds); the engine reads it as a UTC day.
    date: Any
    # The album's title, when the photo sits in one.
    album: str | None = None
    # A DASH video's audio stream, merged into it.
    audio: str | None = None


def media_items(data: Any, stamp: Any = None, seen: set[str] | None = None, *,
                owner: str | None = None) -> Iterator[Item]:
    """Every photo and video anywhere in a result, each once, with the date of the nearest dated node.

    With `owner`, media another account owns (a shared post, a suggested reel) is left out.

    Args:
        data: the JSON to search; a list is searched item by item.
        stamp: the date of the nearest dated node above `data`, used when `data` has none of its own.
        seen: the ids already yielded, shared with the recursion; None starts a new set.
        owner: the profile's id, or None to keep every account's media.
    """
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
    """A video node's best file: its DASH video and audio, the HD progressive one, or else the best playable URL.

    Args:
        node: the JSON node, which is a video only when its type or its files say so.
        stamp: the date to file it under, from `posted_on` or its parent.

    Returns:
        The item; None when the node isn't a video or has no id or no URL.
    """
    typename = node.get("__typename", "")
    if not ("Video" in typename or "Reel" in typename or "progressive_urls" in node):
        return None
    video_id = str(node.get("id") or node.get("video_id") or "")
    if video_id and (streams := dash.best(selector.parse(node).jmespath(MANIFEST).first)):
        return Item(video_id, streams.video, "video", stamp, audio=streams.audio)
    progressive = selector.parse(node).jmespath(PROGRESSIVE).all
    # The HD file is picked by its quality tag; without one, the first progressive file is the fallback.
    hd = [entry for entry in progressive if selector.parse(entry).jmespath("metadata.quality").first == "HD"]
    url = next((entry.get("progressive_url") for entry in hd + progressive if entry.get("progressive_url")), None)
    url = url or next((node[key] for key in VIDEO_URLS if node.get(key)), None)
    return Item(video_id, url, "video", stamp) if video_id and url else None


def photo_item(node: dict, stamp: Any) -> Item | None:
    """A photo node's largest image, with its album's name when it has one.

    Args:
        node: the JSON node, which is a photo only when its type or its image says so.
        stamp: the date to file it under, from `posted_on` or its parent.

    Returns:
        The item; None when the node isn't a photo, is a grid tile, or has no id or no URL.
    """
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
    """A node's own stamp, from the first of its timestamp fields that `day` reads as a date; None when it has none.

    A counter or a placeholder (0, a small number) in a timestamp field isn't a date, and `day`
    refuses it, so the node keeps its nearest dated parent's.
    """
    return next((node[key] for key in TIMESTAMPS if day(node.get(key)) is not None), None)


def image_extension(url: str) -> str:
    """An image's extension: the format its `stp=dst-<format>` asks the CDN for, or else its path's."""
    parts = urlsplit(url)
    # The CDN converts the file to the format its `stp=dst-<format>` names, so that is the extension.
    converted = re.search(r"dst-(jpg|png|webp|gif)", parse_qs(parts.query).get("stp", [""])[0])
    if converted:
        return converted[1]
    suffix = PurePosixPath(parts.path).suffix.lower().lstrip(".")
    return suffix if suffix in ("jpg", "png", "webp", "gif") else "jpg"
