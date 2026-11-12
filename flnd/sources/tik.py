"""TikTok: a profile's videos, photo posts, stories, playlists, reposts, liked and favorites, and its picture.

  - A profile is opened in a browser. When TikTok shows a captcha, a person can solve it in the
    window; when nobody does, the source backs off as after a rate limit and loads the page again.
  - The profile is scrolled while /api/post/item_list and the story list are read; the Reposts,
    Favorites and Liked tabs are then opened and scrolled the same way. Liked and Favorites show
    only when the account made them public; otherwise one line says so.
  - Full MP4 clips and photo-mode carousels are saved. A missing createTime is read from the id
    itself (a Snowflake: the upper 32 bits).
  - One video, by id or URL, is read from the public embed page, without a browser.
  - CDN requests send tt_chain_token, sessionid and the Referer, which avoids 403 Forbidden.

Folder layout, under `.flnd/downloads/tik/<username>/`:
  videos/<YYYY-MM-DD>_<video_id>.mp4
  images/<YYYY-MM-DD>_<post_id>_<idx>.jpg
  stories/<YYYY-MM-DD>_<story_id>.mp4 (or .jpg)
  albums/<playlist_title>/<YYYY-MM-DD>_<video_id>.mp4
  reposts/, liked/, favorites/   <YYYY-MM-DD>_<video_id>.mp4 (or _<idx>.jpg)
  profile/avatar_<id>.jpg
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Dict, List, NamedTuple
from urllib.parse import urlsplit

from flnd.session import Session
from flnd.sources.base import FetchError, Source
from flnd.target import Target, link, name, unreadable
from shared import fmt, selector

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
VIDEO_ID_DIGITS = 16
# Seconds to let the profile settle before looking for a captcha, and to wait for a tab to take a click.
SETTLE = 5.0
CLICK_TIMEOUT = 10.0
# The profile's data, server-rendered into the page.
PAGE_DATA = "__UNIVERSAL_DATA_FOR_REHYDRATION__"
USER = '"__DEFAULT_SCOPE__"."webapp.user-detail".userInfo'


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
# The profile tabs opened after the videos: (category, the tab's data-e2e name).
TABS = (("reposts", "repost-tab"), ("favorites", "favorites-tab"), ("liked", "liked-tab"))


def feed_of(response: Any) -> Feed | None:
    return next((feed for feed in FEEDS if feed.api in response.url), None)


def is_feed(response: Any) -> bool:
    return feed_of(response) is not None


def has_captcha(session: Session) -> bool:
    page = session.page.selector
    return bool(page.css(CAPTCHA_CSS)) or bool(CAPTCHA_TEXT.search(" ".join(page.xpath(VISIBLE_TEXT).all)))


class TikTok(Source):
    """TikTok profiles in a browser, and single videos from the embed page."""

    code = "tik"
    name = "TikTok"
    domains = ("tiktok.com",)
    short_links = ("vm.tiktok.com", "vt.tiktok.com")
    kinds_of_target = ("user", "video")
    examples = ("tik:example_creator", "tik:@example_creator", "https://www.tiktok.com/@example_creator",
                "https://www.tiktok.com/@example_creator/video/7301234567890123456", "tik:7301234567890123456")
    needs = ("sessionid", "sid_tt")
    categories = ("videos", "stories", "playlists", "reposts", "liked", "favorites", "profile")
    default_categories = ("videos", "stories", "playlists", "profile")
    user_agent = TIKTOK_UA
    browser_args = ("--no-sandbox",)

    def resolve(self, text: str) -> Target:
        """A user name, or one video: a URL with /video/ or /photo/, or an id of 16 or more digits."""
        found = link(text, self.domains)
        if found is None:
            if text.isdigit() and len(text) >= VIDEO_ID_DIGITS:
                return Target(self.code, "video", text)
            user = name(text)
        else:
            parts = found.parts
            for kind, video_id in zip(parts, parts[1:]):
                if kind in ("video", "photo") and video_id.isdigit():
                    return Target(self.code, "video", video_id)
            user = name(parts[0]) if parts and parts[0].startswith("@") else None
        if not user:
            raise unreadable(self, text)
        return Target(self.code, "user", user)

    def run(self, session: Session) -> None:
        if session.target.kind == "video":
            self._download_single_embed(session, session.target.value)
        else:
            self._download_profile(session, session.target.value)

    def _download_single_embed(self, session: Session, video_id: str) -> None:
        """Queue a single video or photo post via TikTok public embed endpoint."""
        embed_url = f"https://www.tiktok.com/embed/v2/{video_id}"
        try:
            page = session.fetch_html(embed_url, timeout=20.0, follow_redirects=False)
        except FetchError as error:
            session.warn("{err} on embed endpoint for {id}", err=error, id=video_id)
            return
        data = page.script("__NEXT_DATA__").first
        item_struct = selector.select(data).jmespath("props.pageProps.itemStruct").first if data else None
        if not item_struct:
            session.warn("Could not extract item data from embed HTML for {id}", id=video_id)
            return
        for item in self._extract_tiktok_items({"itemList": [item_struct]}, category="videos"):
            self._save_media_item(session, item)

    def _download_profile(self, session: Session, username: str) -> None:
        """Open the profile, wait out any captcha, then read each wanted list to its end."""
        counts = {feed.category: 0 for feed in FEEDS}
        has_more = [True]

        def on_items(session: Session, data: Any, response: Any) -> None:
            feed = feed_of(response)
            if feed is None or session.folder is None or not isinstance(data, dict):
                return
            if feed.category == "videos" and "hasMore" in data:
                has_more[0] = bool(data.get("hasMore"))
            # Counted whether saved or skipped, so a second run still sees the list grow.
            for item in self._extract_tiktok_items(data, category=feed.category):
                self._save_media_item(session, item)
                counts[feed.category] += 1

        session.on_response(is_feed, on_items)
        profile = f"https://www.tiktok.com/@{username}"
        if not self._open_profile(session, profile):
            return
        user = session.page.selector.script(PAGE_DATA).jmespath(USER).first or {}
        if session.wants(category="profile"):
            self._save_avatar(session, selector.select(user).jmespath("user.avatarLarger").first)
        if any(session.wants(category=category) for category in ("videos", "stories", "playlists")):
            # A captcha can cover the grid after the page loaded; the list then never comes.
            while True:
                session.scroll(count=lambda: counts["videos"], more=lambda: has_more[0], delay=2.5)
                if counts["videos"] or not has_captcha(session):
                    break
                self._wait_out_captcha(session, profile)
            listed = selector.select(user).jmespath("stats.videoCount").first
            session.info("videos: {n} found; the profile lists {listed}", n=counts["videos"], listed=listed)
            if not counts["videos"] and session.wants(category="videos"):
                self._download_linked_posts(session)
        for category, tab in TABS:
            if session.wants(category=category):
                self._read_tab(session, category, tab, lambda: counts[category])
        session.folder = None

    def _open_profile(self, session: Session, url: str) -> bool:
        """Load the profile, and wait out any captcha it shows; False when it didn't load."""
        if not session.goto(url):
            return False
        # The captcha shows a few seconds after the load event, over a page that looks ready.
        session.page.raw.wait_for_timeout(SETTLE * 1000)
        self._wait_out_captcha(session, url)
        return True

    def _wait_out_captcha(self, session: Session, url: str) -> None:
        """Until no captcha is left: a person solves it in the window, or the source backs off and reloads."""
        while has_captcha(session):
            if session.handoff(until=lambda: not has_captcha(session),
                               message="Solve the TikTok captcha in the browser window", timeout=60.0):
                return
            session.challenged()
            session.goto(url)
            session.page.raw.wait_for_timeout(SETTLE * 1000)

    def _read_tab(self, session: Session, category: str, tab: str, count: Any) -> None:
        """Open one profile tab and scroll it to its end; one line when the account doesn't show it."""
        css = f'[data-e2e="{tab}"]'
        if not session.page.selector.css(css):
            session.info("{tab}: not public on this account", tab=category)
            return
        try:
            session.page.raw.locator(css).first.click(timeout=CLICK_TIMEOUT * 1000)
        # Playwright's errors can't be named here (sources never import it); a tab that won't open is skipped.
        except Exception as error:
            session.warn("{tab}: the tab didn't open: {error}", tab=category, error=str(error).splitlines()[0])
            return
        session.scroll(count=count, delay=2.5)
        if not count():
            session.info("{tab}: nothing public on this account", tab=category)

    def _download_linked_posts(self, session: Session) -> None:
        """When the API was walled, read each post linked on the page from its embed page."""
        linked_ids = [match.group(2) for href in session.page.selector.css(POST_LINKS).all
                      if (match := re.search(r"/(video|photo)/(\d+)", href))]
        for video_id in dict.fromkeys(linked_ids):
            self._download_single_embed(session, video_id)

    def _save_avatar(self, session: Session, url: str | None) -> None:
        if not url:
            return
        # The path's last part is "<id>~<size options>.jpeg"; the id names the picture.
        picture_id = PurePosixPath(urlsplit(url).path).name.split("~")[0]
        session.save(url, f"avatar_{picture_id}.jpg", category="profile", folder="profile",
                     headers=self._cdn_headers(session))

    def _save_media_item(self, session: Session, item: Dict[str, Any]) -> bool:
        """Queue a TikTok clip or photo into its category's folder with anti-403 CDN headers."""
        category = item["category"]
        playlist = item.get("playlist")
        kind = item["kind"]

        if category == "videos" and playlist:
            dest_folder, category = f"albums/{fmt.filename(playlist)}", "playlists"
        elif category == "videos":
            dest_folder = "videos" if kind == "video" else "images"
        else:
            dest_folder = category

        ext = "mp4" if kind == "video" else "jpg"
        date_str = item.get("date") or ""
        idx = item.get("idx", 0)
        item_id = item["id"]
        stem = f"{item_id}_{idx}" if idx > 0 else item_id
        filename = f"{date_str}_{stem}.{ext}" if date_str else f"{stem}.{ext}"
        key = f"{item_id}_{idx}" if idx > 0 else item_id
        return session.save(item["url"], filename, category=category,
                            date=date.fromisoformat(date_str) if date_str else None, folder=dest_folder,
                            key=key, headers=self._cdn_headers(session))

    def _cdn_headers(self, session: Session) -> Dict[str, str]:
        # TikTok's CDN requires the Referer and the session cookies (tt_chain_token) to avoid 403 Forbidden.
        token = session.cookies.get("tt_chain_token", "")
        sess = session.cookies.get("sessionid", "")
        cookie_header = f"tt_chain_token={token}; sessionid={sess}" if token else f"sessionid={sess}"
        return {"Referer": "https://www.tiktok.com/", "User-Agent": TIKTOK_UA, "Cookie": cookie_header}

    def _extract_tiktok_items(self, data: Dict[str, Any], *, category: str) -> List[Dict[str, Any]]:
        """Parse itemList / storyList from TikTok API into structured media items."""
        items = []
        raw_list = selector.select(data).jmespath("itemList || storyList || story_list").all

        for raw in raw_list:
            item_id = str(raw.get("id") or raw.get("aweme_id") or "")
            if not item_id:
                continue
            date_str = posted_on(raw, item_id)
            playlist_name = selector.select(raw).jmespath("playlist.name || playlist_info.name").first
            common = {"id": item_id, "date": date_str, "category": category, "playlist": playlist_name}

            # 1. Photo mode carousel
            images = selector.select(raw).jmespath("imagePost.images").all
            if images:
                for idx, img in enumerate(images):
                    url_list = selector.select(img).jmespath(
                        "imageURL.urlList || displayImage.urlList || thumbnail.urlList").all
                    if url_list:
                        items.append({**common, "url": url_list[0], "kind": "image", "idx": idx + 1})
            # 2. Video post
            elif video_url := best_video(raw.get("video") or {}):
                items.append({**common, "url": video_url, "kind": "video", "idx": 0})

        return items


def posted_on(raw: Dict[str, Any], item_id: str) -> str:
    """The post's day as YYYY-MM-DD, from createTime or else from the id's upper 32 bits; "" when neither."""
    create_time = raw.get("createTime") or raw.get("create_time")
    if isinstance(create_time, str) and create_time.isdigit():
        create_time = int(create_time)
    # UTC, as twt and tlg: a local day would name the same video differently, and put it on the
    # other side of --since, on two machines (fix #15, 2026-11). 100000000 (1973) tells a real
    # Unix time from a placeholder 0 or a small number.
    if isinstance(create_time, (int, float)) and create_time > 100000000:
        return datetime.fromtimestamp(create_time, timezone.utc).strftime("%Y-%m-%d")
    stamp = int(item_id) >> 32 if item_id.isdigit() else 0
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d") if stamp > 100000000 else ""


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
                  and (urls := selector.select(entry).jmespath("PlayAddr.UrlList").all)]
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
