"""Facebook: a profile's posts, photos, tagged photos, albums, reels, videos, stories and pictures,
and a single video, photo or post by its link.

Everything is read in a browser that carries the saved login cookies. Facebook's web app (Comet)
embeds GraphQL results in its pages, inside `<script type="application/json">` blocks holding
`__bbox`, and asks its GraphQL endpoint for more while a page scrolls, answered as one JSON
value per line. This module reads both; it never calls the GraphQL API itself.

Who calls it: the engine, through the routes and categories below (`flnd.job.posts`), and the
`date` hook for a post the listing left undated. It calls only `flnd.source` and the stdlib.

The flow of a profile (`fbk:example.page`):
1. The `user` route loads the profile page once and keeps its embedded results and the
   responses that came while it loaded. From them it reads the profile header: the profile's
   id, its pictures, its story buckets, and the paths of its tabs (`Profile`).
2. `posts` reads that page's results without loading it again, then scrolls it; `photos`,
   `tagged`, `reels` and `videos` each open their tab and scroll it. A tab's scroll ends when its
   `page_info` says there is no next page (`tab_cursor`), or when the bottom stays quiet.
3. `albums` scrolls the albums tab, collects each album's link, then scrolls each album.
4. `stories` opens each story bucket the header lists; `profile` takes the header's pictures.
5. Every result is walked for photos and videos (`media_items`): a video's best DASH streams or
   its progressive files, a photo's largest image. Media another account owns is left out.

A single link (`video`, `watch`, `photo`, `post` routes) loads its one page the same way and
yields the media it names. An `fb.watch` short link (`watch`) is first opened in the browser,
and the video's id read from where it lands. `photo.php?fbid=` is the `photo` route, and
`permalink.php` / `story.php` links are read by `resolve`; none of the site's own pages is ever
taken for a profile's name, and one that lacks the value it needs is refused with what it lacks.

Folder layout under `.flnd/media/fbk/<target>/`:
  posts/, photos/, tagged/, reels/, videos/   <date>_<id>.jpg (or .mp4)
  albums/<album_title>/                       <date>_<photo_id>.jpg
  stories/                                    <date>_<id>.jpg (or .mp4)
  profile/                                    profile_<id>.jpg, cover_<id>.jpg
  (a single video, photo or post)             <date>_<id>.jpg (or .mp4), in the target's folder

How login works: `login = cookies("c_user", "xs")`. `flnd login fbk` reads Facebook's cookies
from a browser or a cookie file, and either cookie proves a login. The engine loads them into
the browser each run, so every page here is read as that account. A login is optional: a
logged-out browser gets what Facebook shows the public, which is little. A page that lands on
`/login` or `/login.php` (a login wall) is waited out `WALL_ATTEMPTS` times, then stops the
target with a `LoginError` that says to log in again.

Site quirks, each with the day it was first seen (flnddocs/sources/fbk has them for people):
- 2026-11-11: a tab's path differs by profile (`photos_by` or `photos`, `reels_tab` or
  `reels`, `videos_by` or `videos`); the one the header lists is used (`TABS`, `tab_path`).
- 2026-11-11: a video's progressive files moved under `videoDeliveryResponseFragment`; older
  pages keep them on the node, so both places are read (`PROGRESSIVE`, `MANIFEST`).
- 2026-11-11: a grid tile (`TimelineAppCollectionItem`) holds a small copy of a photo that an
  inner node has in full, so the tile is skipped (`TILE`). A video's thumbnails are images
  inside its node, and are never taken for photos.
- 2026-11-11: other accounts' media (a comment's photo, a shared post) shows in a profile's
  tabs; media whose owner id isn't the profile's is left out.
- 2026-11-11: the CDN converts an image to the format its `stp=dst-<format>` parameter names,
  which is therefore the file's extension (`image_extension`).
- 2026-11-12: a throttled session gets a page saying "temporarily blocked", or a GraphQL answer
  that is Comet's error page (`CometErrorRoot.react`); both are waited out (`BLOCKED`,
  `COMET_ERROR`).
- 2026-11-12: a comment list carries its own `page_info`, deeper than the tab's; only one at
  most `PAGE_INFO_DEPTH` objects under `data` is the tab's next page.
- 2026-11, live: on some loads the profile's Tiles feed answers first with "no next page",
  which says nothing about the tab (`tab_cursor`).
- 2026-11-13: a photo's own day is taken to be the first `created_time` on its page; not yet
  checked by hand on a live page (`CREATED_TIME`).

Rules a change must keep:
- The history key of a photo or video is its id alone (`key = "{post}"`), so a photo found in two
  tabs is saved once. Changing it downloads every saved file again.
- Early stops (a sync that has caught up, `--when`) are the engine's: a category only yields its
  posts in listing order. The ends kept here are the site's own (`page_info`, a quiet bottom).
- A block or a login wall read from a page is waited out with `site.back_off`, a bounded number
  of times, and never taken for an empty tab.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from itertools import chain
from pathlib import PurePosixPath
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlsplit

from flnd.source import (
    Blocked,
    File,
    LoginError,
    NotFound,
    Post,
    Site,
    Source,
    Target,
    UsageError,
    cookies,
    dig,
    target,
)

SITE = "https://www.facebook.com"
# What a link Facebook has no route for here is refused with: a profile is what is read.
SINGLE_POST = (
    "this kind of link isn't supported yet; give the profile, or a video, photo.php or "
    "permalink.php link"
)
# The CDN refuses a request without the site's Referer.
CDN_HEADERS = {"Referer": f"{SITE}/"}
# The site's own pages, which a user-name pattern must never read as a profile's name: a single
# video, photo or post, the login page, and the paths the other routes read.
NOT_NAMES = (
    r"profile\.php", "people", "share", "watch", "reel", "reels", "videos", "photo",
    r"photo\.php", r"permalink\.php", r"story\.php", "login", r"login\.php",
)
USER_NAME = r"(?!(?:" + "|".join(NOT_NAMES) + r")$)[\w.]+"
# The pages that name one post by `story_fbid=` and its owner's `id=`; `resolve` reads them,
# since a pattern captures one value and these need two.
PERMALINKS = ("permalink.php", "story.php")
# The pages that name one photo by `fbid=`, which the `photo` route reads.
PHOTO_PAGES = ("photo.php", "photo")


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
# Where a video's progressive files are: Facebook moved them under its delivery fragment
# (seen 2026-11-11), and older pages still have them on the node.
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
TILE = "TimelineAppCollectionItem"
# The profile header's cover and pictures are the profile category's, not any tab's.
NOT_MEDIA = {"profile_header_renderer"}
# Date fields in order of preference; `posted_on` takes the first that holds a Unix time.
TIMESTAMPS = ("creation_time", "publish_time", "post_timestamp", "timestamp", "created_time")
# The Comet GraphQL endpoint's responses carry the feed; any other response is not read.
FEED = "graphql"
# A photo page's own day: taken to be the first `created_time` it holds. Unverified until a hand
# check on a live page; a wrong guess only dates a photo that would otherwise be estimated.
CREATED_TIME = re.compile(r'"created_time":(\d{9,11})\b')
# A video's id in the URL a video link lands on: `/watch/?v=<id>`, `/<page>/videos/<id>`,
# `/videos/<id>`, `/reel/<id>`.
VIDEO_PATH = re.compile(r"/(?:videos|reel|reels)/(?:[^/]+/)?(\d+)")
# What the page says when Facebook rate-limits a session.
BLOCKED = "temporarily blocked"
# What the countdown says while a block or a login wall is waited out.
TEMPORARILY_BLOCKED = "Facebook says you've been temporarily blocked"
LOGIN_WALL = "Facebook asked for a login (if it keeps asking, run `flnd login fbk` again)"
# How many times a login wall and a block are waited out before giving up. `site.back_off`
# raises `Blocked` itself after three rounds in a row; these bound the loops here as well, since
# a wall and a block in turn are not "in a row" for it.
WALL_ATTEMPTS = 2
BLOCK_ATTEMPTS = 3
# What a GraphQL answer holds instead of data when Facebook blocks it: Comet's error page.
COMET_ERROR = "CometErrorRoot.react"
# How deep under a line's `data` a tab's `page_info` sits (data → node → connection → page_info);
# a deeper one is a comment list's or a nested story's, which isn't the tab's next page.
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
    """Facebook profiles and single videos, photos and posts, read in a logged-in browser."""

    code = "fbk"
    name = "Facebook"
    domains = ("facebook.com", "fb.com", "fb.watch")
    api = 1
    # Not required: a logged-out browser gets what Facebook shows the public, which is little.
    login = cookies("c_user", "xs")
    # No published limit: the learned gap doubles on a throttle and is saved, so the first block
    # teaches it; the floor only keeps a burst slower than the other sites'.
    pace = 0.2
    # A file's folder is where it was found: `part` is its tab (or "videos" or "albums"), `album`
    # the album's title when it has one. A single link's files have neither.
    folder = "{part}/{album}"
    fields = ("part", "album")
    # `<date>_<id>`, or `<id>` for an undated file: the names fbk always gave.
    filename = "{date}_{post}"
    key = "{post}"
    # Refused before any route. Each page is exact; `*/posts` also refuses `/posts/<id>`.
    unsupported = {
        f"{host}/{path}": SINGLE_POST for host in ("facebook.com", "fb.com")
        for path in ("*/posts", "photos", "groups", "events", "stories")
    }

    def date(self, site: Site, post: Post) -> Any:
        """A photo's day, from its own page's first `created_time`.

        Args:
            site: the target's site; the request is sent from its tab.
            post: the undated post; its id is the photo's (or video's) id.

        Returns:
            Unix seconds, or None when the page holds no `created_time`.
        """
        # Asked from the tab, as the page itself does, so it carries the feed's session.
        page = site.fetch(f"{SITE}/photo/?fbid={post.id}", in_page=True).text
        found = CREATED_TIME.search(page)
        return int(found[1]) if found else None

    def resolve(self, text: str) -> Target:
        """Read a `permalink.php` or `story.php` link as a post; anything else by the routes.

        Those links name a post by two query values, `story_fbid=` (the post) and `id=` (its
        owner), which no pattern can capture together. The post's target value is
        `<id>_<story_fbid>`; neither part holds an underscore.

        The site's own pages (`photo.php`, `permalink.php`, …) are never a profile's name: a
        `photo.php` link without a numeric `fbid=`, and `fbk:photo.php` typed as a name, are
        refused here with what the link needs, rather than read as the user "photo.php".

        Args:
            text: the target's text after `fbk:`, or a link.

        Returns:
            The resolved target.

        Raises:
            UsageError: a permalink lacks one of the two values, a photo link its `fbid=`, a
                name is one of the site's pages, or nothing reads `text`.
        """
        link = text if text.lower().startswith(("http://", "https://")) else f"https://{text}"
        parts = urlsplit(link)
        host = (parts.hostname or "").lower()
        page = parts.path.strip("/")
        on_site = any(host == d or host.endswith("." + d) for d in ("facebook.com", "fb.com"))
        if "/" not in text and not text.lower().startswith("http"):
            # A bare name (`fbk:<name>`): one of the site's pages is no profile.
            if re.fullmatch("|".join(NOT_NAMES), text.lstrip("@"), re.IGNORECASE):
                raise UsageError(f"fbk: {text!r} is a page of Facebook, not a profile; "
                                 "give the whole link")
            return super().resolve(text)
        if on_site and page in PERMALINKS:
            query = parse_qs(parts.query)
            story = (query.get("story_fbid") or [""])[0]
            owner = (query.get("id") or [""])[0]
            if not re.fullmatch(r"\w+", story) or not owner.isdigit():
                raise UsageError(
                    f"fbk: a {page} link names its post by story_fbid= and id=; "
                    "this one lacks one of them"
                )
            return Target(f"fbk:{owner}_{story}", kind="post")
        if on_site and page in PHOTO_PAGES:
            # The `photo` route reads a numeric `fbid=`; anything else would match no route.
            fbid = (parse_qs(parts.query).get("fbid") or [""])[0]
            if not fbid.isdigit():
                raise UsageError(f"fbk: a {page} link names its photo by fbid=<number>; "
                                 "this one has none")
        return super().resolve(text)

    @target(
        r"facebook.com/watch?v={video:\d+}", r"fb.com/watch?v={video:\d+}",
        r"facebook.com/*/videos/{video:\d+}", r"fb.com/*/videos/{video:\d+}",
        r"facebook.com/videos/{video:\d+}", r"fb.com/videos/{video:\d+}",
        r"facebook.com/reel/{video:\d+}", r"fb.com/reel/{video:\d+}",
        r"facebook.com/reels/{video:\d+}", r"fb.com/reels/{video:\d+}",
    )
    def video(self, site: Site, video: str) -> Iterator[Post]:
        """One video or reel, by its link: `https://www.facebook.com/watch/?v=<id>`."""
        yield from read_single(site, f"{SITE}/watch/?v={video}", wanted=video)

    @target(r"fb.watch/{code:[\w-]+}")
    def watch(self, site: Site, code: str) -> Iterator[Post]:
        """One video by its `fb.watch` short link, followed in the logged-in browser.

        The short link redirects to the video's page (`/watch/?v=<id>`, `/reel/<id>` or
        `/<page>/videos/<id>`), where the video's id is read. Opening it in the browser rather
        than with a plain request keeps the login, which a private video's redirect needs. The
        files are keyed by the video's id, so the same video through another link isn't saved
        twice.

        Args:
            site: the target's site; the short link is opened in its tab.
            code: the short link's code, the path after `fb.watch/`.

        Yields:
            The video's post, as the `video` route gives it.

        Raises:
            LoginError: the link led to Facebook's login page.
            NotFound: the link didn't lead to a video's page.
        """
        link = f"https://fb.watch/{code}/"
        landed = site.url if site.open(link) else ""
        video = video_id(landed)
        if video is None and _on_login(landed):
            raise LoginError(f"fbk: {link} led to Facebook's login page",
                             hint="run flnd login fbk, then try again")
        if video is None:
            raise NotFound(f"fbk: {link} didn't lead to a video",
                           hint="check the link in a browser")
        yield from read_single(site, f"{SITE}/watch/?v={video}", wanted=video)

    @target(
        r"facebook.com/photo.php?fbid={photo:\d+}", r"fb.com/photo.php?fbid={photo:\d+}",
        r"facebook.com/photo?fbid={photo:\d+}", r"fb.com/photo?fbid={photo:\d+}",
    )
    def photo(self, site: Site, photo: str) -> Iterator[Post]:
        """One photo, by its `photo.php?fbid=<id>` or `photo/?fbid=<id>` link."""
        yield from read_single(site, f"{SITE}/photo/?fbid={photo}", wanted=photo)

    @target(shown=f"{SITE}/permalink.php?story_fbid=<post>&id=<user>")
    def post(self, site: Site, post: str) -> Iterator[Post]:
        """One post's photos and videos, by its `permalink.php` or `story.php` link (`resolve`).

        Media another account owns (a comment's photo) is left out by the post owner's id.

        Args:
            site: the target's site.
            post: the target's value, `<owner id>_<story_fbid>`, as `resolve` made it.

        Yields:
            A post per photo and video the post's owner owns.
        """
        owner, _, story = post.partition("_")
        url = f"{SITE}/permalink.php?story_fbid={story}&id={owner}"
        yield from read_single(site, url, owner=owner)

    @target(
        "fbk:{user}",
        r"facebook.com/profile.php?id={user:\d+}", r"fb.com/profile.php?id={user:\d+}",
        r"facebook.com/people/*/{user:\d+}", r"fb.com/people/*/{user:\d+}",
        r"facebook.com/@{user}", r"fb.com/@{user}",
        f"facebook.com/{{user:{USER_NAME}}}", f"fb.com/{{user:{USER_NAME}}}",
    )
    def user(self, site: Site, user: str) -> Profile:
        """A profile by user name or id: `fbk:example.page`, loaded once for every category.

        The page is loaded here, with the GraphQL feed watched from before it loads, so the
        responses of its first load are kept for `posts`. A page that didn't load gives a
        `Profile` with no results and no header: each category then loads its own tab, and
        `stories` and `profile` are skipped with a warning.

        Args:
            site: the target's site; the profile is opened in its tab.
            user: the user name (`example.page`) or the numeric id the link or target gave.

        Returns:
            The loaded profile, which every category of the target receives.

        Raises:
            LoginError: Facebook kept showing its login page.
            Blocked: Facebook kept saying the session is temporarily blocked.
        """
        url = f"{SITE}/{user}"
        # Subscribed before the page loads, so the responses of its first load are kept too.
        with site.feed(FEED, lines=True) as arrived:
            results = read_page(site, url)
            # Waits for the network to go quiet once, which a page that didn't load doesn't need.
            early = list(arrived) if results is not None else []
        header = next(
            (h for result in results or [] if (h := read_header(result)) is not None), None,
        )
        return Profile(url, results, early, header)

    # The posts tab is the profile page itself, which the route left open: it comes first,
    # since the tabs after it navigate away.
    @user.category
    def posts(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile's own posts: photos and videos from the page and its scroll."""
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
        """The albums tab, then each album it lists, into `albums/<title>/`.

        The albums tab is scrolled to its end first, collecting each album tile's link and
        title; media on the tab itself is yielded as it comes. Then the albums start loading in
        background tabs, and each is scrolled in turn (`read_album`). A profile whose header
        lists no albums tab is skipped with a note.

        Args:
            site: the target's site.
            profile: the loaded profile; its header gives the tab's path and the owner's id.

        Yields:
            A post per photo and video, filed under `albums/<album title>/`.
        """
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
        site.warm(list(listed))
        for url, title in listed.items():
            yield from read_album(site, url, title=title, owner=profile.owner)

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
        """The stories of each story bucket the header lists: only those active now.

        Each bucket is its own page (`/stories/<bucket>/`), loaded once with no scroll: a
        bucket's cards are all in the page's embedded results. A card's date dates every media
        it attaches. A bucket page that didn't load is passed over.

        Args:
            site: the target's site.
            profile: the loaded profile; without a header there are no buckets, and the
                category is skipped with a warning.

        Yields:
            A post per photo and video of each story, filed under `stories/`.
        """
        if profile.header is None:
            site.warn("the profile header wasn't found; its stories are skipped")
            return
        urls = [f"{SITE}/stories/{bucket}/" for bucket in profile.header.story_buckets]
        site.warm(urls)
        for url in urls:
            results = read_page(site, url)
            if results is None:
                continue
            cards = [
                card for result in results for card in dig(result, STORY_CARDS)
                if isinstance(card, dict)
            ]
            for card in cards:
                stamp = posted_on(card)
                for media in dig(card, "attachments[].media"):
                    if not isinstance(media, dict):
                        continue
                    item = video_item(media, stamp) or photo_item(media, stamp)
                    if item is not None:
                        yield post_of(item, "stories")
            site.info("stories: {n} in {url}", n=len(cards), url=url)

    @user.category(folder="profile", key="")
    def profile(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile picture and the cover photo, named `profile_<id>` and `cover_<id>`.

        Their key is empty, so each is remembered by its path: a new picture is a new file.

        Args:
            site: the target's site.
            profile: the loaded profile; without a header the category is skipped with a
                warning.

        Yields:
            A post per picture the header has, the profile picture first.
        """
        if profile.header is None:
            site.warn("the profile header wasn't found; its pictures are skipped")
            return
        for kind, picture in (("profile", profile.header.picture), ("cover", profile.header.cover)):
            if picture:
                url, photo_id = picture
                name = f"{kind}_{photo_id}.{image_extension(url)}"
                yield Post(photo_id, [File(url, name=name)], headers=CDN_HEADERS)


# ---------------------------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------------------------

def read_page(site: Site, url: str) -> list[Any] | None:
    """Load a page and return its embedded results; [] for an empty page, None when it didn't load.

    A login wall (the tab lands on `/login`) and a block page ("temporarily blocked") are each
    waited out with `site.back_off` and the page loaded again, `WALL_ATTEMPTS` and
    `BLOCK_ATTEMPTS` times; the counts never reset each other, so a wall and a block in turn
    still give up.

    Args:
        site: the target's site.
        url: the page.

    Returns:
        The page's embedded results; [] when it loaded but embeds none (an empty tab); None
        when the tab isn't on the page after loading it, so it didn't load.

    Raises:
        LoginError: the login wall stayed.
        Blocked: the block stayed.
    """
    walls = blocks = 0
    while True:
        results = meta_results(site, url)
        if results:
            return results
        # Where the tab is tells a page that didn't load from a refusal and from an empty page.
        if _on_login(site.url):
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


def meta_results(site: Site, url: str) -> list[Any]:
    """Load a page and return the GraphQL results it embeds, or [] when it didn't load."""
    if not site.open(url):
        return []
    return embedded_results(site.document())


def embedded_results(page: Any) -> list[Any]:
    """Every GraphQL result a page embeds, each shaped like a response: `{"data": {…}}`.

    Args:
        page: the parsed page (`site.document()`).

    Returns:
        The results of every `<script type="application/json">` block holding `__bbox`, in page
        order. A block that isn't JSON holds none; the page's other blocks still count.
    """
    return [result for block in page.blocks("__bbox") for result in _boxed(block)]


def _boxed(node: Any) -> Iterator[Any]:
    """Each `__bbox.result` under `node`, depth first; a box's own result isn't searched again.

    Args:
        node: parsed JSON from one of the page's script blocks, or any part of it.

    Yields:
        Each box's result, in page order.
    """
    if isinstance(node, dict):
        box = node.get("__bbox")
        if isinstance(box, dict) and "result" in box:
            yield box["result"]
            return
        for value in node.values():
            yield from _boxed(value)
    elif isinstance(node, list):
        for value in node:
            yield from _boxed(value)


def _on_login(url: str) -> bool:
    """Whether `url` is Facebook's login page (a login wall).

    The first path segment only: a profile named like `/login.example` is no wall.
    """
    return urlsplit(url).path.split("/")[1:2] in (["login"], ["login.php"])


def _place(url: str) -> tuple[str, str]:
    """A URL's path and query, without a trailing slash: where a tab is, whatever its host."""
    parts = urlsplit(url)
    return parts.path.rstrip("/"), parts.query


def video_id(url: str) -> str | None:
    """The video id a video page's URL names (`?v=`, `/videos/<id>`, `/reel/<id>`).

    Args:
        url: where the tab is, such as the page an `fb.watch` link landed on.

    Returns:
        The id's digits; None for a URL off Facebook, or one that names no video.
    """
    parts = urlsplit(url)
    if not (parts.hostname or "").endswith(("facebook.com", "fb.com")):
        return None
    given = (parse_qs(parts.query).get("v") or [""])[0]
    if given.isdigit():
        return given
    found = VIDEO_PATH.search(parts.path)
    return found[1] if found else None


def read_single(site: Site, url: str, *, wanted: str | None = None,
                owner: str | None = None) -> Iterator[Post]:
    """The media of one video, photo or post page, filed in the target's own folder.

    A video's or photo's page also shows suggestions and comments, so only the media whose id
    is `wanted` is kept; a post's page keeps every media its owner owns.

    Args:
        site: the target's site.
        url: the page.
        wanted: the one photo's or video's id to keep; None keeps every media.
        owner: the post owner's id; media another account owns is left out. None keeps all.

    Yields:
        A post per media kept, with no category: the target's own folder.

    Raises:
        NotFound: the page didn't load, or held none of the media asked for (it may be
            private, deleted, or need a login).
    """
    with site.feed(FEED, lines=True) as arrived:
        results = read_page(site, url)
        early = list(arrived) if results is not None else []
    seen: set[str] = set()
    found = [
        item for result in chain(results or [], early)
        for item in media_items(result, seen=seen, owner=owner)
        if wanted is None or item.id == wanted
    ]
    if not found:
        raise NotFound(f"fbk: {url} shows no photo or video",
                       hint="it may be private or deleted; if you can see it, run flnd login fbk")
    for item in found:
        yield post_of(item, "")


def read_album(site: Site, url: str, *, title: str, owner: str | None) -> Iterator[Post]:
    """Scroll one album page and stream its media into `albums/<title>/`.

    Args:
        site: the target's site.
        url: the album's page, as its tile on the albums tab links it.
        title: the album's title, its folder under `albums/`.
        owner: the profile's id: media another account owns is left out. None keeps all.

    Yields:
        A post per photo and video, as the album scrolls.
    """
    with site.feed(FEED, scroll=True, lines=True, cursor=tab_cursor()) as responses:
        results = read_page(site, url)
        if results is None:
            return
        for result in chain(results, responses):
            yield from media_posts(result, "albums", album=title, owner=owner)


def read_tab(site: Site, profile: Profile, tab: Tab) -> Iterator[Post]:
    """The posts of one tab, streamed while it scrolls to its end.

    The tabs after it start loading in background tabs meanwhile (`site.warm`). A tab the
    profile header doesn't list is skipped with a note. One `seen` set spans the whole tab, so
    a media that two results of the scroll both hold is yielded once.

    Args:
        site: the target's site.
        profile: the loaded profile: its URL, its header's tab paths and owner id, and, for
            the posts tab, the page's first results.
        tab: the tab to read.

    Yields:
        A post per photo and video, filed by `media_posts`.
    """
    path = tab_path(tab, profile.paths)
    if path is None:
        site.info("{tab}: this profile has no such tab", tab=tab.category)
        return
    upcoming = TABS[TABS.index(tab) + 1:]
    site.warm([f"{profile.url}/{p}" for t in upcoming if (p := tab_path(t, profile.paths))])
    seen: set[str] = set()
    for result, item_owner in _collect_tab(site, profile, path):
        yield from media_posts(result, tab.category, seen=seen, owner=item_owner)


def _collect_tab(site: Site, profile: Profile, path: str) -> Iterator[tuple[Any, str | None]]:
    """Scroll one tab to its end, yielding `(result, owner)` pairs as they come.

    The posts tab's first batch is the profile page the route loaded, not filtered by owner:
    the header that names the owner came from the same results. A Comet error answer in the
    scroll is a block, waited out up to `BLOCK_ATTEMPTS` times in a row; any good answer
    resets the count.

    The feed is subscribed before the tab loads, so no response of its first load is lost. It
    ends where `tab_cursor` says the tab has no next page, or when the bottom stays quiet.

    Args:
        site: the target's site.
        profile: the loaded profile. Its results are the posts tab's first batch, unless the
            route's page didn't load, when the posts tab is loaded again here.
        path: the tab's path under the profile's URL; "" for the posts tab.

    Yields:
        `(result, owner)`: a GraphQL result, and the owner id whose media it may hold (None
        keeps every media).

    Raises:
        Blocked: the Comet error answer came `BLOCK_ATTEMPTS` + 1 times in a row.
        LoginError: the tab's page kept showing the login wall (from `read_page`).
    """
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


# ---------------------------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------------------------

def page_info(line: Any) -> tuple[bool, str | None] | None:
    """A result line's `(has a next page, cursor)`, or None when it says nothing about paging.

    The `page_info` is looked for in `data` at most `PAGE_INFO_DEPTH` objects deep, never inside
    a list, which is where the tab's connection keeps it and a comment list's never is.

    The search is breadth first, so the shallowest `page_info` wins. A `page_info` counts only
    when it holds `end_cursor` or `has_next_page`; a next page needs both a true
    `has_next_page` and a cursor.

    Args:
        line: one parsed line of a GraphQL answer, or anything else (which says nothing).

    Returns:
        `(True, cursor)` when there is a next page, `(False, cursor or None)` when there isn't,
        or None when the line holds no `page_info` within reach.
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
    """A cursor reader for one scrolled tab's feed (`site.feed(cursor=…)`).

    It is `page_info`, except that a page with no next one ends the feed only after some page
    said there was one. 2026-11, live: on some loads the profile's Tiles feed (its intro photos,
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


# ---------------------------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------------------------

def media_posts(
    result: Any, tab: str, *, album: str | None = None, owner: str | None = None,
    seen: set[str] | None = None,
) -> Iterator[Post]:
    """Each photo and video of `owner` in one result of a tab, filed by where it belongs.

    A photo that names its album goes under `albums/<album>/`, and a video found in a tab other
    than reels or videos under `videos/`, whichever tab found it.

    Args:
        result: one GraphQL result; anything but an object holds nothing.
        tab: the category of the tab being read, the folder its own media goes in.
        album: the title of the album being read, which files every media under it; None
            outside an album, when only a photo's own album name does.
        owner: the profile's id: media another account owns is left out. None keeps all.
        seen: the ids already yielded, shared across one tab's results so a media is filed
            once; None starts a new set for this result alone.

    Yields:
        A post of one file per photo and video kept.
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


def post_of(item: Item, part: str, *, album: str | None = None) -> Post:
    """One item as a post of one file, which the templates name `<date>_<id>.<ext>`.

    A DASH video has no name: the engine gives a joined video its extension. Any other file is
    named `<id>.<ext>` for its extension; the engine puts the post's day in front of that stem,
    as `filename` says.

    Args:
        item: the photo or video found.
        part: the folder under the target (`photos`, `videos`, `albums`, ...); "" for a single
            link's files, which sit in the target's own folder.
        album: the album's title, the folder under `albums/`; None outside an album.

    Returns:
        The post, with the CDN's Referer header that its file needs.
    """
    if item.audio is not None:
        file = File(item.url, audio=item.audio)
    else:
        first = item.url if isinstance(item.url, str) else item.url[0]
        extension = "mp4" if item.kind == "video" else image_extension(first)
        file = File(item.url, name=f"{item.id}.{extension}")
    return Post(item.id, [file], date=item.date, headers=CDN_HEADERS, part=part, album=album)


def album_links(data: Any) -> dict[str, str]:
    """The albums a result lists, as {url: title}: each album's tile on the albums tab.

    A tile is a `TimelineAppCollectionItem` whose node is an `Album` and which has a link.

    Args:
        data: one GraphQL result of the albums tab.

    Returns:
        Each album's page URL and its title ("album" for an untitled one), in listing order.
    """
    return {
        tile["url"]: dig(tile, "title.text") or "album"
        for tile in dig(data, f"..[__typename={TILE}]")
        if dig(tile, "node.__typename") == "Album"
        and tile.get("url")
    }


def tab_path(tab: Tab, paths: set[str]) -> str | None:
    """A tab's path on this profile: the first the header names, or its first without a header."""
    if not tab.paths[0] or not paths:
        return tab.paths[0]
    return next((path for path in tab.paths if path in paths), None)


def read_header(result: Any) -> Header | None:
    """The profile header's id, pictures, story buckets and tab paths, or None without one.

    The tab paths are the `tab_key` of each navigation section of the header, and of each
    collection inside a section: the photos tab, for one, is a collection of its section.

    Args:
        result: one GraphQL result of the profile page.

    Returns:
        The header, or None when the result holds none at `HEADER`. A picture without its
        photo id is named by "picture" or "cover" instead.
    """
    user = dig(result, HEADER)
    if not isinstance(user, dict):
        return None
    picture = dig(user, "profilePicLarge.uri")
    picture_id = dig(user, "profilePhoto.id")
    cover = dig(user, "cover_photo.photo.image.uri")
    cover_id = dig(user, "cover_photo.photo.id")
    sections = [
        section for section in dig(user, "profile_tabs.profile_user.timeline_nav_app_sections"
                                         ".edges[].node")
        if isinstance(section, dict)
    ]
    paths = {section.get("tab_key") for section in sections} | {
        collection.get("tab_key") for section in sections
        for collection in dig(section, "all_collections.nodes") or []
        if isinstance(collection, dict)
    }
    buckets = [
        str(node["id"]) for node in dig(user, "story_bucket.nodes") or []
        if isinstance(node, dict) and node.get("id")
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
    # The file's URL: a DASH video's video stream, an image, or a progressive video's every
    # address, HD first, as mirrors.
    url: str | tuple[str, ...]
    # "image" or "video".
    kind: str
    # When it was posted, as the site gives it (Unix seconds); the engine reads it as a UTC day.
    date: Any
    # The album's title, when the photo sits in one.
    album: str | None = None
    # A DASH video's audio stream, joined with it.
    audio: str | None = None


def media_items(
    data: Any, stamp: Any = None, seen: set[str] | None = None, *,
    owner: str | None = None,
) -> Iterator[Item]:
    """Every photo and video in a result, each once, dated by its nearest dated node.

    Args:
        data: a result, or any part of one.
        stamp: the date of the nearest dated node above `data`.
        seen: the ids found so far, shared across results so a media is found once.
        owner: the profile's id: media whose owner is another id is left out. None keeps all.

    Yields:
        Each photo and video, depth first in the result's order. A video's node isn't searched
        further, and neither is the profile header (`NOT_MEDIA`).
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
    owned_by = dig(data, "owner.id || video_owner.id")
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
    """A video node's best file: its DASH streams, else its progressive files and URLs.

    A node is a video when its `__typename` names a video or a reel, or when it has
    progressive files. Its id is `id`, else `video_id`.

    Args:
        node: one object of a result.
        stamp: the date to give the video (its own, or its nearest dated parent's).

    Returns:
        The video's item: its DASH video stream with `audio` set, or its progressive URLs as
        mirrors, HD first. None for a node that isn't a video, or has no id or no file.
    """
    typename = node.get("__typename", "")
    if not ("Video" in typename or "Reel" in typename or "progressive_urls" in node):
        return None
    video_id = str(node.get("id") or node.get("video_id") or "")
    if video_id and (joined := File.dash(dig(node, MANIFEST))) is not None:
        # A DASH video gets no name: the engine names it from `filename` (`<date>_<id>`) and
        # gives the joined file `.webm` for a WebM pair, `.mp4` otherwise, as fbk always did.
        return Item(video_id, joined.url, "video", stamp,  # type: ignore[arg-type]
                    audio=joined.audio)
    progressive = [entry for entry in dig(node, PROGRESSIVE) or [] if isinstance(entry, dict)]
    # The HD file comes first by its quality tag, then the other progressive files, then the
    # node's own URLs (HD before SD): every one a mirror, tried in order when one refuses.
    hd = [entry for entry in progressive if dig(entry, "metadata.quality") == "HD"]
    found = (
        [entry.get("progressive_url") for entry in hd + progressive]
        + [node.get(key) for key in VIDEO_URLS]
    )
    urls = tuple(dict.fromkeys(url for url in found if isinstance(url, str) and url))
    if not video_id or not urls:
        return None
    return Item(video_id, urls[0] if len(urls) == 1 else urls, "video", stamp)


def photo_item(node: dict, stamp: Any) -> Item | None:
    """A photo node's largest image, with its album's name when it has one.

    A node is a photo when its `__typename` names a photo, or when it has one of the image
    fields `PHOTO_URL` reads. Its id is `id`, `photo_id` or `legacy_photo_id`.

    Args:
        node: one object of a result.
        stamp: the date to give the photo (its own, or its nearest dated parent's).

    Returns:
        The photo's item, or None for a grid tile, a node that isn't a photo, or one with no
        id or no image URL.
    """
    # A tab's grid tile holds a small copy of a photo that its inner node has in full.
    if node.get("__typename") == TILE:
        return None
    has_image = isinstance(node.get("image"), dict) and "uri" in node["image"]
    if not ("Photo" in node.get("__typename", "") or has_image
            or any(key in node for key in ("large_share_image", "viewer_image", "photo_image"))):
        return None
    photo_id = str(node.get("id") or node.get("photo_id") or node.get("legacy_photo_id") or "")
    url = dig(node, PHOTO_URL)
    if not photo_id or not isinstance(url, str) or not url:
        return None
    return Item(photo_id, url, "image", stamp, dig(node, "album.name"))


def posted_on(node: dict) -> Any:
    """A node's own date: the first timestamp field holding a Unix time, as given, or None.

    The engine reads the date; this only tells a stamp from a missing or zero one, which
    Facebook gives for undated media.

    Args:
        node: one node of a result.

    Returns:
        The first of `TIMESTAMPS` above 0, a number or its digits as text; None for none.
    """
    for key in TIMESTAMPS:
        value = node.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit() and int(value) > 0:
            return value
    return None


def image_extension(url: str) -> str:
    """An image's extension, from its `stp=dst-<format>` parameter or its path; jpg otherwise.

    Args:
        url: the image's CDN URL.

    Returns:
        "jpg", "png", "webp" or "gif", without a dot.
    """
    parts = urlsplit(url)
    # The CDN converts the file to the format `stp=dst-<format>` names, so that is its extension.
    converted = re.search(r"dst-(jpg|png|webp|gif)", parse_qs(parts.query).get("stp", [""])[0])
    if converted:
        return converted[1]
    suffix = PurePosixPath(parts.path).suffix.lower().lstrip(".")
    return suffix if suffix in ("jpg", "png", "webp", "gif") else "jpg"
