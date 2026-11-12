"""Instagram: a profile's posts, reels, tagged posts, reposts, stories, highlights and picture, or one post.

Everything is read in a browser logged in with the saved cookies. Each profile tab is scrolled
to its end while its GraphQL responses are watched (`watch_responses`). A listed post without its
date or files (the reels, tagged and reposts grids list only thumbnails) becomes a lazy post: its
own page is read, where the first load embeds the post in the HTML, only when it isn't saved yet.
Stories and highlights are read from their pages the same way. A JSON body asking to "wait a few
minutes" makes the source back off before it scrolls on.

How it works: `user` returns a `Profile`, which every category receives. The tabs fill it with the
profile's picture and highlights as they are read, so `stories`, `highlights` and `profile` load the
profile's page only when no tab did. Each category yields `Post`s; the engine names and keys them
with the formats below, which equal what the source wrote before routes, except for the date,
which is now the UTC day. A file whose URL doesn't end in its extension, and every file of a
lazy post, is named here instead (`File(name=)`) so its name stays the same.

Folder layout, under `.flnd/downloads/ins/<username>/`:
  posts/, reels/, tagged/, reposts/   <YYYY-MM-DD>_<code>_<idx>.jpg (or .mp4; no _<idx> for one file)
Videos are saved from their DASH manifest when they have one: the best video, merged with its audio.
  stories/                            <YYYY-MM-DD>_<story_id>.jpg (or .mp4)
  highlights/<title>/                 <YYYY-MM-DD>_<story_id>.jpg (or .mp4)
  profile/                            the profile picture, named as Instagram names it
One post, `ins:https://www.instagram.com/p/<code>/`, lands in `.flnd/downloads/ins/<code>/`.

History keys: `<code>_<idx>` for a tab's or a single post's files, the path for stories, highlights
and the picture.

A post found on two tabs (a reel is on the grid too) is saved once, on the first.

    saved = collect(Instagram, "ins:example_user", pages=PAGES, options=Options(categories=("stories",)))
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import chain
from pathlib import PurePosixPath
from typing import Any, Iterator
from urllib.parse import urlsplit

from flnd.source import File, Post, Site, Source, day, target
from flnd.sources.common import dash
from flnd.sources.common.relay import embedded_results
from flnd.sources.common.watch import watch_responses
from shared import fmt, selector

SITE = "https://www.instagram.com"
# The profile's tabs, in the order they are read, and their paths under the profile.
TABS = {"posts": "", "reels": "reels/", "tagged": "tagged/", "reposts": "reposts/"}
# The CDN refuses a file request that doesn't come from the site.
CDN_HEADERS = {"Referer": f"{SITE}/"}
# Where each profile tab's result lists its posts; the name Instagram uses now comes first.
TAB_CONNECTIONS = {
    "posts": ("xdt_api__v1__feed__user_timeline_graphql_connection",),
    "reels": ("fetch__XDTUserDict.clips_connection", "xdt_api__v1__clips__user__connection_v2"),
    "tagged": ("xdt_api__v1__usertags__user_id__feed_connection",),
    "reposts": ("fetch__XDTUserDict.user_reposts_timeline", "xdt_api__v1__feed__user_reposts_graphql_connection"),
}
IMAGE_EXTENSIONS = {"jpg", "webp", "png", "heic"}
# One post's page: a reel's, or any other post's.
POST = "data.xig_fetch_media || data.xdt_api__v1__media__shortcode__web_info.items[0]"
# A story page lists one user's active stories; a highlight's page lists the highlight's.
STORIES = ("data.xdt_api__v1__feed__reels_media__connection.edges[].node.items[]"
           " || data.xdt_api__v1__feed__reels_media.reels_media[].items[]")
# What Instagram's JSON says when it is rate limiting, seen 2026-11:
# {"message": "Please wait a few minutes before you try again.", "status": "fail"}.
THROTTLED = "wait a few minutes"


def is_graphql(url: str) -> bool:
    return "graphql" in url


class Instagram(Source):
    """Instagram profiles and single posts, read in a browser."""

    code = "ins"
    name = "Instagram"
    domains = ("instagram.com",)
    needs = ("sessionid",)
    # A post's category, and a highlight's title: "posts", "highlights/<title>", or "" for a single post.
    folder = "{category}/{album}"
    # Every story link, a user's or a highlight's: a story expires, but the profile lists them all.
    unsupported = {"instagram.com/stories": "story links aren't supported; give the profile, and its stories come with it"}

    # `post` comes before `user`, so instagram.com/p/<code> is never the user "p". A post's code is
    # base64url, so it takes `-` as well as `\w`.
    @target(r"instagram.com/p/{code:[\w-]+}", r"instagram.com/reel/{code:[\w-]+}", r"instagram.com/reels/{code:[\w-]+}",
            r"instagram.com/tv/{code:[\w-]+}", r"instagram.com/*/p/{code:[\w-]+}", r"instagram.com/*/reel/{code:[\w-]+}",
            r"instagram.com/*/reels/{code:[\w-]+}", r"instagram.com/*/tv/{code:[\w-]+}")
    def post(self, site: Site, code: str) -> Iterator[Post]:
        """One post or reel, by its code: its page is read, and every carousel item comes with it."""
        media = post_page(site, code)
        if media is not None:
            yield post_of(media)

    @target("{user}", "instagram.com/{user}")
    def user(self, site: Site, user: str) -> Profile:
        """A profile, by its user name; a tab's URL (/reels/, /tagged/) is its profile too.

        Nothing is loaded yet: a tab, or the first category that needs the profile's page, loads it.
        """
        return Profile(user)

    @user.category(newest_first=True)
    def posts(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile's posts, newest first, scrolled to the end of the grid."""
        yield from profile.read_tab(site, "posts")

    @user.category(newest_first=True)
    def reels(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile's reels, newest first; the grid lists only thumbnails, so each reel is read from its page."""
        yield from profile.read_tab(site, "reels")

    @user.category
    def tagged(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The posts the profile is tagged in, by other people: the grid follows the tags, not the dates."""
        yield from profile.read_tab(site, "tagged")

    @user.category
    def reposts(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The posts the profile reposted, in the order of the reposts."""
        yield from profile.read_tab(site, "reposts")

    @user.category(key="")
    def stories(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile's active stories; their key is their path, as before routes."""
        yield from story_posts(site, f"{SITE}/stories/{profile.user}/", "stories")

    @user.category(key="")
    def highlights(self, site: Site, profile: Profile) -> Iterator[Post]:
        """Each highlight's stories, in the highlight's folder; their key is their path."""
        profile.describe(site)
        for highlight_id, title in profile.highlights:
            for post in story_posts(site, f"{SITE}/stories/highlights/{highlight_id}/", f"highlights/{fmt.filename(title)}"):
                yield post.replace(album=title)

    @user.category(folder="profile", key="")
    def profile(self, site: Site, profile: Profile) -> Iterator[Post]:
        """The profile picture, named as Instagram names it, so a new picture is a new file; its key is its path."""
        profile.describe(site)
        if profile.picture:
            yield Post(profile.user, [File(profile.picture, name=picture_name(profile.picture))], headers=CDN_HEADERS)


@dataclass
class Profile:
    """What the profile's results told so far: its picture, highlights, and which posts the tabs listed.

    Args:
        user: the user name.
    """

    user: str
    # The largest profile picture, once a result describing the user came.
    picture: str | None = None
    described: bool = False
    highlights: list[tuple[str, str]] = field(default_factory=list)
    # The codes each tab listed, so a post a result repeats is yielded once.
    listed: dict[str, set[str]] = field(default_factory=lambda: {tab: set() for tab in TABS})
    # The codes a grid listed whole, so a reel the posts grid already lists isn't opened again.
    saved: set[str] = field(default_factory=set)

    def read_tab(self, site: Site, tab: str) -> Iterator[Post]:
        """Load one tab and scroll it to its end, yielding the posts its results list.

        Args:
            site: the target's site.
            tab: one of `TABS`.

        The responses are watched from before the page loads, so the first results, which the
        page's HTML embeds, and the later ones, which scrolling fetches, are read the same way.
        """
        with watch_responses(site, is_graphql, scroll=True) as responses:
            if site.goto(f"{SITE}/{self.user}/{TABS[tab]}"):
                for result in chain(embedded_results(site.page.document), responses):
                    if throttled(result):
                        site.back_off("Instagram asked to wait a few minutes")
                        continue
                    self.note(result)
                    yield from self._posts(site, result, tab)
        site.info("{tab}: {posts} listed", tab=tab, posts=fmt.count(len(self.listed[tab]), "post"))

    def describe(self, site: Site) -> None:
        """Load the profile's own page for its picture and highlights, unless a tab's results gave them."""
        if self.described:
            return
        if site.goto(f"{SITE}/{self.user}/"):
            for result in embedded_results(site.page.document):
                self.note(result)

    def note(self, result: Any) -> None:
        """Note the profile's picture and highlights from one result."""
        if not isinstance(result, dict):
            return
        # Only the profile's first result describes its user; later pages may describe others.
        picture = profile_picture(result)
        if picture and not self.described:
            self.picture, self.described = picture, True
        self.highlights += [found for found in listed_highlights(result) if found not in self.highlights]

    def _posts(self, site: Site, result: Any, tab: str) -> Iterator[Post]:
        """The posts of one result this tab didn't list before: whole ones, or lazy ones read from their page."""
        if not isinstance(result, dict):
            return
        for media in tab_media(result, tab):
            code = post_name(media)
            if not code or code in self.listed[tab]:
                continue
            self.listed[tab].add(code)
            if is_complete(media):
                self.saved.add(code)
                if post_files(media):
                    yield post_of(media)
            elif code not in self.saved:
                # The grid lists only a thumbnail: the date and files come from the post's own page, once it is wanted.
                yield Post(code, lambda code=code: page_files(site, code), headers=CDN_HEADERS)


def throttled(result: Any) -> bool:
    """Whether a response's JSON is Instagram asking to slow down instead of a result."""
    return isinstance(result, dict) and THROTTLED in str(result.get("message", ""))


def post_page(site: Site, code: str) -> dict | None:
    """Open one post's page and return the post it embeds, or None (with a warning) when it holds none."""
    if not site.goto(f"{SITE}/p/{code}/"):
        return None
    found = [media for result in embedded_results(site.page.document) if (media := post_media(result))]
    if not found:
        site.warn("post {code}: its page holds no post; skipped", code=code)
        return None
    return found[0]


def page_files(site: Site, code: str) -> list[File]:
    """The files of a grid post, read from its page; none when the page holds no post or it is outside `--time`."""
    media = post_page(site, code)
    if media is None or day(media.get("taken_at")) not in site.time:
        return []
    # The grid gave the post no date, so the names carry the page's.
    return files_of(media, pinned=True)


def post_of(media: dict) -> Post:
    """A post with its date and files read: the engine reads `taken_at` as the date."""
    return Post(post_name(media), files_of(media), date=media.get("taken_at"), headers=CDN_HEADERS)


def files_of(media: dict, *, pinned: bool = False) -> list[File]:
    """Each file of a post, named as before routes: `<date>_<code>[_<n>].<ext>`.

    Args:
        media: the post's JSON.
        pinned: name every file here, for a post whose date the engine doesn't know. Otherwise
            only a file whose URL doesn't end in its extension is: the formats would give it another.
    """
    found = post_files(media)
    code, taken = post_name(media), day(media.get("taken_at"))
    files = []
    for index, (url, extension, audio) in enumerate(found):
        stem = f"{code}_{index}" if len(found) > 1 else code
        named = pinned or PurePosixPath(urlsplit(url).path).suffix.lower() != f".{extension}"
        files.append(File(url, audio=audio, name=file_name(taken, stem, extension) if named else None))
    return files


def story_posts(site: Site, url: str, folder: str) -> Iterator[Post]:
    """Open a story or highlight page and yield one post for each of its stories.

    Args:
        site: the target's site.
        url: the stories' page.
        folder: what the stories are called in the log: "stories", "highlights/<title>".
    """
    if not site.goto(url):
        return
    stories = [story for result in embedded_results(site.page.document) for story in story_items(result)]
    for story in stories:
        file = best_file(story)
        if file is None:
            continue
        source, extension, audio = file
        story_id = str(story.get("pk") or story.get("id", "")).split("_")[0]
        # The key is the path, so the extension in the name has to be the one the file is saved with.
        name = file_name(day(story.get("taken_at")), story_id, extension)
        yield Post(story_id, [File(source, audio=audio, name=name)], date=story.get("taken_at"), headers=CDN_HEADERS)
    site.info("{folder}: {stories}", folder=folder, stories=fmt.count(len(stories), "story", plural="stories"))


def file_name(taken: Any, stem: str, extension: str) -> str:
    """A file's name: `2026-01-02_C1abc_0.jpg`, or `C1abc_0.jpg` for a post without a date."""
    return f"{taken}_{stem}.{extension}" if taken else f"{stem}.{extension}"


def post_name(post: dict) -> str:
    """A post's code, as in its URL, or its number when the result leaves the code out."""
    return str(post.get("code") or post.get("pk") or "")


def picture_name(url: str) -> str:
    """A profile picture's file name, as Instagram's URL names it, so a new picture is a new file."""
    return urlsplit(url).path.rsplit("/", 1)[-1] or "profile.jpg"


def tab_media(result: Any, tab: str) -> list[dict]:
    """The posts one profile tab's result lists: `tab_media(result, "reels")`."""
    found = selector.parse(result)
    for path in TAB_CONNECTIONS[tab]:
        connection = found.jmespath(f"data.{path}").first
        if isinstance(connection, dict):
            break
    else:
        return []
    if "repost_grid_items" in connection:
        nodes = [item.get("media", item) for item in connection["repost_grid_items"]]
    else:
        nodes = [edge.get("node") or {} for edge in connection.get("edges", [])]
    # The reels tab wraps each post as {"media": post}.
    return [node["media"] if isinstance(node.get("media"), dict) else node for node in nodes]


def is_complete(media: dict) -> bool:
    """Whether a listed post has its date and files; the reels, tagged and reposts grids leave both out."""
    return bool(media.get("taken_at"))


def post_media(result: Any) -> dict | None:
    """The post a post's own page shows, or None when this result isn't it."""
    media = selector.parse(result).jmespath(POST).first
    return media if isinstance(media, dict) else None


def story_items(result: Any) -> list[dict]:
    """The stories of a story or highlight page."""
    return [item for item in selector.parse(result).jmespath(STORIES).all if isinstance(item, dict)]


def listed_highlights(result: Any) -> list[tuple[str, str]]:
    """The profile's highlights as `(id, title)`, from the result that lists them."""
    nodes = selector.parse(result).jmespath("data.highlights.edges[].node").all
    return [(str(node["id"]).removeprefix("highlight:"), node.get("title") or "highlight")
            for node in nodes if isinstance(node, dict) and node.get("id")]


def profile_picture(result: Any) -> str | None:
    """The largest profile picture of the profile's user, from the result that describes them."""
    return selector.parse(result).jmespath("data.user.hd_profile_pic_url_info.url").first


def post_files(media: dict) -> list[tuple[str, str, str | None]]:
    """Each file of a post as `(url, extension, audio)`: every carousel item, or the post itself."""
    children = media.get("carousel_media") or [media]
    return [file for child in children if (file := best_file(child)) is not None]


def best_file(media: dict) -> tuple[str, str, str | None] | None:
    """One item's best file as `(url, extension, audio)`: its DASH video, its largest progressive one, or its largest image.

    `audio` is the DASH audio stream's URL, merged into the video; None for anything else.
    """
    if streams := dash.best(media.get("video_dash_manifest")):
        return streams.video, streams.extension[1:], streams.audio
    videos = media.get("video_versions") or []
    if videos:
        return max(videos, key=_area)["url"], "mp4", None
    images = selector.parse(media).jmespath("image_versions2.candidates").all
    if images:
        url = max(images, key=_area)["url"]
        return url, image_extension(url), None
    return None


def image_extension(url: str) -> str:
    """An image's extension from its URL: older posts are served as WebP, newer ones as JPEG."""
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower().lstrip(".")
    return suffix if suffix in IMAGE_EXTENSIONS else "jpg"


def _area(version: dict) -> int:
    return (version.get("width") or 0) * (version.get("height") or 0)
