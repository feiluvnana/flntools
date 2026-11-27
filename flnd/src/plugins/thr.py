"""Threads on plugin API 2: a profile's posts, replies, reposts and picture, and one post by its
link, read from the pages. SPEC.md#thr-threads.
"""

from __future__ import annotations

import random
import re
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from flnd.plugin import (
    Blocked,
    Browser,
    Changed,
    Ctx,
    File,
    LoginError,
    NotFound,
    Plugin,
    Post,
    Response,
    Stream,
    Target,
    UsageError,
    best,
    cookies,
    dig,
    headline,
    meta,
    page_info,
    route,
    wall,
)

SITE = "https://www.threads.com"
# The profile's tabs and their paths under `/@<user>` (confirm live).
TABS = {"posts": "", "replies": "/replies", "reposts": "/reposts"}
# The page's own GraphQL answers carry each tab's next pages (confirm live).
FEED = "graphql"
# Where a profile tab's or a post page's result lists its threads: each item's `post`
# (confirm live).
POSTS = ("data.mediaData.edges[].node.thread_items[].post"
         " || data.data.edges[].node.thread_items[].post"
         " || data.containing_thread.thread_items[].post"
         " || data.data.containing_thread.thread_items[].post")
_MEDIA_PAGES = page_info("data.mediaData.page_info")
_DATA_PAGES = page_info("data.data.page_info")


def TAB_PAGES(result: Any) -> tuple[bool, str | None] | None:
    """Pagination cursor from either `data.mediaData.page_info` or `data.data.page_info`."""
    return _MEDIA_PAGES(result) or _DATA_PAGES(result)
# The user a profile result describes, and the picture sizes it lists (confirm live).
USER = "data.user || data.xdt_user_by_username"
PICTURE = "hd_profile_pic_versions"
# What Instagram's stack says when it rate limits, as `ins` reads it (confirm live).
THROTTLED = "wait a few minutes"
THROTTLE_REASON = "Threads asked to wait a few minutes"
IMAGE_EXTENSIONS = {"jpg", "webp", "png", "heic"}
USER_NAME = re.compile(r"[A-Za-z0-9._]{1,30}")
NOT_A_PROFILE = "that's not a profile"
LOGIN_HINT = "run flnd auth login thr"
LOCK_WALLS = ("/challenge", "/accounts/suspended", "/accounts/disabled", "/consent")
_FATAL_META = ("checkpoint_required", "challenge_required", "login_required", "consent_required")


def _check_meta_error(data: Any) -> None:
    """Abort immediately on Meta checkpoint, login, consent, or action-block JSON payloads."""
    if not isinstance(data, dict):
        return
    msg = str(data.get("message", "")).lower()
    err = str(data.get("error_type", "")).lower()
    if (data.get("require_login") is True or data.get("checkpoint_url")
            or data.get("challenge") or any(k in msg or k in err for k in _FATAL_META)):
        raise LoginError(
            "thr: account requires action or login; resolve in browser",
            hint=LOGIN_HINT, retryable=False,
        )
    if (data.get("spam") is True or data.get("feedback_required") is True
            or "feedback_required" in msg or "feedback_required" in err):
        raise Blocked("thr: action blocked by Threads (feedback_required)", retryable=False)


class Threads(Plugin):
    """Threads profiles and single posts, read from the pages."""

    code = "thr"
    name = "Threads"
    domains = ("threads.com", "threads.net")
    # Images, media and fonts load until a live check shows its pages work without them.
    hidden = True
    # Threads uses the Instagram login: logged in when `sessionid` is there (confirm live).
    login = cookies("sessionid")
    browser = Browser.ANY
    # Started as Instagram's, the same stack; measure on a live run.
    pace = 2.0
    walls = ("/login", "/accounts/login")
    fields = ("title",)
    filename = "{date}_{title:.40}_{post}{index}"
    unsupported = {
        f"{host}/{path}": reason
        for host in ("threads.com", "threads.net")
        for paths, reason in (
            (("*/replies", "*/reposts", "*/media"),
             'a profile tab isn\'t a target; write the user with a category, e.g. '
             '"thr:example_user|replies"'),
            (("search",), "there's no search; give a user or a post"),
            (("activity", "settings", "login", "following", "saved", "liked",
              "insights", "about", "privacy", "terms", "direct", "onboarding"), NOT_A_PROFILE),
        )
        for path in paths
    }

    @classmethod
    def resolve(cls, text: str) -> Target | None:
        """The routes' reading, then the name rule: a user name is `USER_NAME`.

        Raises:
            UsageError: a name that isn't one, or one of the site's own pages.
        """
        found = super().resolve(text)
        if found is not None and found.kind == "user" and not USER_NAME.fullmatch(found.value):
            raise UsageError(f"thr: {found.value!r} isn't a user name; "
                             "a user name is 1 to 30 letters, digits, . or _")
        return found

    def blocked(self, response: Response) -> float | None:
        """Abort on lock walls or Meta error payloads; hold on "please wait"."""
        found = wall(getattr(response, "url", ""), LOCK_WALLS)
        if found is not None:
            raise LoginError(
                f"thr: account requires action ({found}); resolve in browser",
                hint=LOGIN_HINT, retryable=False,
            )
        parsed = _json(response) if getattr(response, "content", b"")[:1] == b"{" else None
        _check_meta_error(parsed)
        return 0.0 if throttled(parsed) else None

    # `post` comes before `user`, so threads.com/@name/post/<code> is never the user "name".
    @route(*((pattern, "post/{code}") for pattern in (
        r"thr:post/{code:[\w-]+}", r"threads.com/*/post/{code:[\w-]+}",
        r"threads.net/*/post/{code:[\w-]+}")), single=True)
    async def post(self, ctx: Ctx, code: str) -> AsyncIterator[Post]:
        """One post: `thr:post/C1abcDEFghi`, or its page, `threads.com/@example_user/post/C1abc`.

        Raises:
            NotFound: the page holds no post of that code, or one without files.
        """
        code = code.removeprefix("post/")
        found = None
        async with aclosing(results(ctx, f"{SITE}/t/{code}", scroll=False)) as listing:
            async for result in listing:
                found = next((media for media in posts_of(result)
                              if media.get("code") == code), None)
                if found is not None:
                    break
        if found is None:
            raise NotFound(f"thr: post {code}: its page holds no post")
        if not post_files(found):
            raise NotFound(f"thr: post {code} has no files")
        yield post_of(found)

    @route("thr:{user}", "threads.com/{user}", "threads.net/{user}")
    async def user(self, ctx: Ctx, user: str) -> Profile:
        """A profile: `thr:example_user`, or `https://www.threads.com/@example_user`."""
        return Profile(user)

    @user.category
    async def posts(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """The threads tab, newest first, to its end."""
        async for post in read_tab(ctx, profile, "posts"):
            yield post

    @user.category
    async def replies(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """The profile's own replies' media, newest first."""
        async for post in read_tab(ctx, profile, "replies"):
            yield post

    @user.category(own=False, newest_first=False)
    async def reposts(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """The posts the profile reposted, each with the original post's date."""
        async for post in read_tab(ctx, profile, "reposts"):
            yield post

    @user.category(newest_first=False, folder="profile", key="{path}")
    async def profile(self, ctx: Ctx, profile: Profile) -> AsyncIterator[Post]:
        """The profile picture, at its largest, under the CDN's own name; keyed by path."""
        await profile.describe(ctx)
        if profile.picture:
            name = urlsplit(profile.picture).path.rsplit("/", 1)[-1] or "profile.jpg"
            yield Post(profile.user, [File(profile.picture, name=name)], title="")


def throttled(result: Any) -> bool:
    """Whether a JSON answer is a "please wait" instead of a result."""
    return isinstance(result, dict) and THROTTLED in str(result.get("message", ""))


def _json(response: Response) -> Any:
    try:
        return response.json
    except Changed:
        return None


# ---------------------------------------------------------------------------------------------
# The profile and its tabs
# ---------------------------------------------------------------------------------------------

class Profile(Mapping[str, Any]):
    """What the profile's pages said so far: its picture, and the codes each tab listed.

    As a mapping it holds `user`, which the engine's templates read.

    Args:
        user: the user name, as the target gives it.
    """

    def __init__(self, user: str) -> None:
        self.user = user
        self.picture: str | None = None
        self.loaded = False
        self.listed: dict[str, set[str]] = {tab: set() for tab in TABS}

    def __getitem__(self, key: str) -> Any:
        if key != "user":
            raise KeyError(key)
        return self.user

    def __iter__(self) -> Iterator[str]:
        return iter(("user",))

    def __len__(self) -> int:
        return 1

    async def describe(self, ctx: Ctx) -> None:
        """Load the profile page once for its picture, unless a tab told it."""
        if self.loaded:
            return
        async for result in results(ctx, f"{SITE}/@{self.user}", scroll=False):
            self.note(result)
        self.loaded = True

    def note(self, result: Any) -> None:
        """Note the picture from one result of the profile's pages."""
        user = dig(result, USER)
        if not isinstance(user, dict):
            return
        found = best(streams(user.get(PICTURE)))
        picture = found.url if found is not None else user.get("profile_pic_url")
        if isinstance(picture, str) and picture:
            self.picture = picture
            self.loaded = True


async def results(ctx: Ctx, url: str, *, scroll: bool) -> AsyncIterator[Any]:
    """Each result of a page: the ones it embeds, then its feed's; a throttle holds.

    A 429 holds and reloads once per progress; a no-progress 429 reload aborts with non-retryable
    `Blocked` instead of looping infinitely.
    """
    seen_codes: set[str] = set()
    reloads = 0
    got_new = False
    while True:
        try:
            async with ctx.feed(FEED, scroll=scroll, cursor=TAB_PAGES, resume=False) as feed:
                await ctx.open(url)
                for result in meta(await ctx.document()):
                    _check_meta_error(result)
                    if throttled(result):
                        await ctx.hold(THROTTLE_REASON, seconds=random.uniform(300.0, 600.0))
                        continue
                    codes = [str(m.get("code") or "") for m in posts_of(result) if m.get("code")]
                    if any(c not in seen_codes for c in codes) or (
                        not scroll and not reloads and isinstance(result, dict)
                        and result.get("data")
                    ):
                        got_new = True
                    seen_codes.update(codes)
                    yield result
                async for result in feed:
                    _check_meta_error(result)
                    if throttled(result):
                        await ctx.hold(THROTTLE_REASON, seconds=random.uniform(300.0, 600.0))
                        continue
                    codes = [str(m.get("code") or "") for m in posts_of(result) if m.get("code")]
                    if any(c not in seen_codes for c in codes) or (
                        not scroll and not reloads and isinstance(result, dict)
                        and result.get("data")
                    ):
                        got_new = True
                    seen_codes.update(codes)
                    yield result
            break
        except Blocked as error:
            if not error.retryable:
                raise
            if (reloads > 0 and not got_new) or reloads >= 6:
                if getattr(ctx, "listing", None) is not None:
                    ctx.listing.stalled = True
                raise Blocked(f"{error} (no progress after reload)",
                              wait=error.wait, retryable=False) from None
            await ctx.hold("rate limited", seconds=error.wait, reset=got_new)
            reloads += 1
            got_new = False


async def read_tab(ctx: Ctx, profile: Profile, tab: str) -> AsyncIterator[Post]:
    """The posts of one tab in its order, scrolled until its `page_info` says it ended.

    Raises:
        NotFound, LoginError: the tab's page didn't load.
    """
    own = profile.user.lower()
    pages_seen = 0
    async with aclosing(results(ctx, f"{SITE}/@{profile.user}{TABS[tab]}", scroll=True)) as pages:
        async for result in pages:
            profile.note(result)
            for media in posts_of(result):
                code = str(media.get("code") or "")
                owner = str(dig(media, "user.username")).lower()
                if (not code or code in profile.listed[tab] or not post_files(media)
                        or (tab != "reposts" and owner != own)):
                    continue
                profile.listed[tab].add(code)
                yield post_of(media)
            state = TAB_PAGES(result)
            # The last page, embedded or scrolled: nothing more is asked for.
            if state is not None and not (state[0] and state[1]):
                return
            if state is not None:
                pages_seen += 1
                delay = (random.uniform(12.0, 25.0) if pages_seen % 15 == 0
                         else random.uniform(2.0, 5.0))
                await ctx.pause(delay, "pausing between tab pages")


# ---------------------------------------------------------------------------------------------
# Posts and their files
# ---------------------------------------------------------------------------------------------

def posts_of(result: Any) -> list[dict]:
    """The posts one result lists, in order; [] for a result that isn't a listing."""
    return [post for post in dig(result, POSTS) or [] if isinstance(post, dict)]


def post_of(media: dict) -> Post:
    """A post with its files read (`taken_at` is its date); a reply is `replies`, else `posts`."""
    reply = dig(media, "text_post_app_info.reply_to_author")
    return Post(str(media["code"]), files_of(media), date=media.get("taken_at"),
                owner=dig(media, "user.username"), category="replies" if reply else "posts",
                title=headline(dig(media, "caption.text")))


def files_of(media: dict) -> list[File]:
    """Each file of a post, named here only where the templates would name it otherwise."""
    found = post_files(media)
    code, taken = str(media["code"]), utc_day(media.get("taken_at"))
    title = headline(dig(media, "caption.text"))[:40]
    files = []
    for n, (url, extension) in enumerate(found):
        stem = f"{code}_{n}" if len(found) > 1 else code
        own = PurePosixPath(urlsplit(url).path).suffix.lower() != f".{extension}"
        name = "_".join(part for part in (taken, title, stem) if part) + f".{extension}"
        files.append(File(url, name=name if own else None))
    return files


def utc_day(value: Any) -> str | None:
    """The UTC day of `taken_at` (Unix seconds) as `YYYY-MM-DD`; None without one."""
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def post_files(media: dict) -> list[tuple[str, str]]:
    """Each file of a post as `(url, extension)`: every carousel item, or the post itself."""
    children = media.get("carousel_media") or [media]
    return [found for child in children if (found := best_file(child)) is not None]


def best_file(media: dict) -> tuple[str, str] | None:
    """An item's best file: its largest MP4, else its largest image; None for neither."""
    video = best(streams(media.get("video_versions")))
    if video is not None:
        return video.url, "mp4"
    image = best(streams(dig(media, "image_versions2.candidates")))
    if image is None:
        return None
    suffix = PurePosixPath(urlsplit(image.url).path).suffix.lower().lstrip(".")
    return image.url, suffix if suffix in IMAGE_EXTENSIONS else "jpg"


def streams(versions: Any) -> list[Stream]:
    """An item's image or video versions as streams, each with its URL and size."""
    if not isinstance(versions, list):
        return []
    return [Stream(version["url"], height=version.get("height") or 0,
                   width=version.get("width") or 0)
            for version in versions if isinstance(version, dict) and version.get("url")]
