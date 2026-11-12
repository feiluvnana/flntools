"""Instagram: a profile's posts, reels, tagged posts, reposts, stories, highlights and picture, or one post.

Everything is read in a browser logged in with the saved cookies. Each profile tab is scrolled
to its end while its GraphQL responses are read. A listed post without its date or files (the
reels, tagged and reposts grids list only thumbnails) is read again from its own page, where the
first load embeds the post in the HTML; stories and highlights are read from their pages the same way.

Folder layout, under `.flnd/downloads/ins/<username>/`:
  posts/, reels/, tagged/, reposts/   <YYYY-MM-DD>_<code>_<idx>.jpg (or .mp4; no _<idx> for one file)
Videos are saved from their DASH manifest when they have one: the best video, merged with its audio.
  stories/                            <YYYY-MM-DD>_<story_id>.jpg (or .mp4)
  highlights/<title>/                 <YYYY-MM-DD>_<story_id>.jpg (or .mp4)
  profile/                            the profile picture, named as Instagram names it
One post, `ins:https://www.instagram.com/p/<code>/`, lands in `.flnd/downloads/ins/<code>/`.

A post found on two tabs (a reel is on the grid too) is saved once, on the first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from flnd import dash
from flnd.session import Session
from flnd.sources.base import Source
from flnd.sources.relay import embedded_results
from flnd.target import Target, TargetError, link, name, unreadable
from shared import fmt, selector

SITE = "https://www.instagram.com"
# The profile's tabs, in the order they are read, and their paths under the profile.
TABS = (("posts", ""), ("reels", "reels/"), ("tagged", "tagged/"), ("reposts", "reposts/"))
# The part of a URL before a post's code: /p/<code>/, /reel/<code>/, and /<user>/p/<code>/ too.
POST_PATHS = {"p", "reel", "reels", "tv"}
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


def is_graphql(response: Any) -> bool:
    return "graphql" in response.url


class Instagram(Source):
    """Instagram profiles and single posts, read in a browser."""

    code = "ins"
    name = "Instagram"
    domains = ("instagram.com",)
    kinds_of_target = ("user", "post")
    examples = ("ins:example_user", "ins:@example_user", "https://www.instagram.com/example_user/",
                "https://www.instagram.com/p/C1abcDEFghi/", "https://www.instagram.com/reel/C1abcDEFghi/")
    needs = ("sessionid",)
    categories = ("posts", "reels", "tagged", "reposts", "stories", "highlights", "profile")
    single_kinds = ("post",)

    def resolve(self, text: str) -> Target:
        """A user name, or one post: a URL with /p/<code>, /reel/<code> or /tv/<code>."""
        found = link(text, self.domains)
        if found is None:
            user = name(text)
        elif (code := post_code(found.parts)) is not None:
            return Target(self.code, "post", code)
        elif found.parts[:1] == ["stories"]:
            raise TargetError(f"{self.code}: story links aren't supported; give the profile, "
                              f"for example {self.examples[0]}, and its stories come with it")
        else:
            user = name(found.parts[0]) if found.parts else None
        if not user:
            raise unreadable(self, text)
        return Target(self.code, "user", user)

    def run(self, session: Session) -> None:
        if session.target.kind == "post":
            save_post_page(session, session.target.value, category="posts", folder="")
            return
        user = session.target.value
        profile = Profile()
        session.on_response(is_graphql, profile.on_result)
        for tab, path in TABS:
            if session.wants(category=tab):
                self._read_tab(session, profile, tab, f"{SITE}/{user}/{path}")
        # The picture and the highlights come with any profile tab; load one if none was wanted.
        if not profile.described and (session.wants(category="profile") or session.wants(category="highlights")):
            session.folder = None
            load(session, profile, f"{SITE}/{user}/")
        session.folder = None
        for tab, code in profile.incomplete:
            # A reel already saved from the grid isn't opened again.
            if code not in profile.saved:
                save_post_page(session, code, category=tab, folder=tab)
        if profile.picture and session.wants(category="profile"):
            session.save(profile.picture, picture_name(profile.picture), category="profile", folder="profile",
                         headers=CDN_HEADERS)
        if session.wants(category="stories"):
            save_story_page(session, f"{SITE}/stories/{user}/", category="stories", folder="stories")
        if session.wants(category="highlights"):
            for highlight_id, title in profile.highlights:
                save_story_page(session, f"{SITE}/stories/highlights/{highlight_id}/", category="highlights",
                                folder=f"highlights/{fmt.filename(title)}")

    def _read_tab(self, session: Session, profile: Profile, tab: str, url: str) -> None:
        session.folder = tab
        if load(session, profile, url):
            session.scroll(count=lambda: len(profile.listed[tab]))
        session.info("{tab}: {n} posts listed", tab=tab, n=len(profile.listed[tab]))


@dataclass
class Profile:
    """What the profile's results told so far: its picture, highlights, and each tab's posts."""

    picture: str | None = None
    described: bool = False
    highlights: list[tuple[str, str]] = field(default_factory=list)
    listed: dict[str, set[str]] = field(default_factory=lambda: {tab: set() for tab, _ in TABS})
    saved: set[str] = field(default_factory=set)
    # Posts listed without their date or files, as (tab, code), read later from their own page.
    incomplete: list[tuple[str, str]] = field(default_factory=list)

    def on_result(self, session: Session, result: Any, response: Any) -> None:
        """Note the profile's picture and highlights, and save the posts of the tab being read."""
        if not isinstance(result, dict):
            return
        # Only the profile's first result describes its user; later pages may describe others.
        picture = profile_picture(result)
        if picture and not self.described:
            self.picture, self.described = picture, True
        self.highlights += [found for found in listed_highlights(result) if found not in self.highlights]
        tab = session.folder
        if tab not in self.listed:
            return
        for post in tab_media(result, tab):
            code = post_name(post)
            if not code or code in self.listed[tab]:
                continue
            self.listed[tab].add(code)
            if is_complete(post):
                save_post(session, post, category=tab, folder=tab)
                self.saved.add(code)
            else:
                self.incomplete.append((tab, code))


def post_code(parts: list[str]) -> str | None:
    """The post code in a URL's path parts, or None when it points to no post."""
    for kind, code in zip(parts, parts[1:]):
        if kind in POST_PATHS:
            return code
    return None


def load(session: Session, profile: Profile, url: str) -> bool:
    """Go to `url` and read the results its HTML embeds, as the later responses are read; False if it didn't load."""
    if not session.goto(url):
        return False
    for result in embedded_results(session.page.selector):
        profile.on_result(session, result, None)
    return True


def save_post_page(session: Session, code: str, *, category: str, folder: str) -> None:
    """Open one post's page and save its files, every carousel item included."""
    if not session.goto(f"{SITE}/p/{code}/"):
        return
    found = [post for result in embedded_results(session.page.selector) if (post := post_media(result))]
    if not found:
        session.warn("post {code}: its page holds no post; skipped", code=code)
        return
    save_post(session, found[0], category=category, folder=folder)


def save_post(session: Session, post: dict, *, category: str, folder: str) -> None:
    """Save each file of a post; the key is its code and place, so a post on two tabs is saved once."""
    code, day = post_name(post), taken_on(post)
    files = post_files(post)
    for index, (url, extension, audio) in enumerate(files):
        stem = f"{code}_{index}" if len(files) > 1 else code
        file_name = f"{day}_{stem}.{extension}" if day else f"{stem}.{extension}"
        session.save(url, file_name, category=category, date=day, folder=folder, key=f"{code}_{index}",
                     headers=CDN_HEADERS, audio=audio)


def save_story_page(session: Session, url: str, *, category: str, folder: str) -> None:
    """Open a story or highlight page and save each of its stories."""
    if not session.goto(url):
        return
    stories = [story for result in embedded_results(session.page.selector) for story in story_items(result)]
    for story in stories:
        file = best_file(story)
        if file is None:
            continue
        url, extension, audio = file
        story_id, day = str(story.get("pk") or story.get("id", "")).split("_")[0], taken_on(story)
        file_name = f"{day}_{story_id}.{extension}" if day else f"{story_id}.{extension}"
        session.save(url, file_name, category=category, date=day, folder=folder, headers=CDN_HEADERS, audio=audio)
    session.info("{folder}: {n} stories", folder=folder, n=len(stories))


def post_name(post: dict) -> str:
    """A post's code, as in its URL, or its number when the result leaves the code out."""
    return str(post.get("code") or post.get("pk") or "")


def picture_name(url: str) -> str:
    """A profile picture's file name, as Instagram's URL names it, so a new picture is a new file."""
    return urlsplit(url).path.rsplit("/", 1)[-1] or "profile.jpg"


def tab_media(result: Any, tab: str) -> list[dict]:
    """The posts one profile tab's result lists: `tab_media(result, "reels")`."""
    found = selector.select(result)
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
    media = selector.select(result).jmespath(POST).first
    return media if isinstance(media, dict) else None


def story_items(result: Any) -> list[dict]:
    """The stories of a story or highlight page."""
    return [item for item in selector.select(result).jmespath(STORIES).all if isinstance(item, dict)]


def listed_highlights(result: Any) -> list[tuple[str, str]]:
    """The profile's highlights as `(id, title)`, from the result that lists them."""
    nodes = selector.select(result).jmespath("data.highlights.edges[].node").all
    return [(str(node["id"]).removeprefix("highlight:"), node.get("title") or "highlight")
            for node in nodes if isinstance(node, dict) and node.get("id")]


def profile_picture(result: Any) -> str | None:
    """The largest profile picture of the profile's user, from the result that describes them."""
    return selector.select(result).jmespath("data.user.hd_profile_pic_url_info.url").first


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
    images = selector.select(media).jmespath("image_versions2.candidates").all
    if images:
        url = max(images, key=_area)["url"]
        return url, image_extension(url), None
    return None


def image_extension(url: str) -> str:
    """An image's extension from its URL: older posts are served as WebP, newer ones as JPEG."""
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower().lstrip(".")
    return suffix if suffix in IMAGE_EXTENSIONS else "jpg"


def taken_on(media: dict) -> date | None:
    """The day a post or story was taken, or None when the result leaves it out."""
    stamp = media.get("taken_at")
    return datetime.fromtimestamp(int(stamp)).date() if stamp else None


def _area(version: dict) -> int:
    return (version.get("width") or 0) * (version.get("height") or 0)
