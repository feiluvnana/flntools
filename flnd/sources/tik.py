"""
TikTok source for flnd.

  - A profile is opened in a browser, which waits for a person to solve a captcha when one
    shows, then scrolls while reading /api/post/item_list and /api/user/story.
  - Full MP4 clips, photo-mode carousels, active stories and playlists are saved.
  - A missing createTime is read from the id itself (a Snowflake: the upper 32 bits).
  - One video, by id or URL, is read from the public embed page, without a browser.
  - CDN requests send tt_chain_token, sessionid and the Referer, which avoids 403 Forbidden.

Folder layout, under `.flnd/downloads/tik/<username>/`:
  videos/<YYYY-MM-DD>_<video_id>.mp4
  images/<YYYY-MM-DD>_<post_id>_<idx>.jpg
  stories/<YYYY-MM-DD>_<story_id>.mp4 (or .jpg)
  albums/<playlist_title>/<YYYY-MM-DD>_<video_id>.mp4
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Callable, Dict, List

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


def is_feed_api(response: Any) -> bool:
    return "/api/post/item_list" in response.url or "/api/user/story" in response.url


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
    categories = ("videos", "stories", "playlists")
    default_categories = ("videos", "stories", "playlists")
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

        try:
            data = page.script("__NEXT_DATA__").first
            if data is None:
                session.warn("Could not extract item data from embed HTML for {id}", id=video_id)
                return
            item_struct = (selector.select(data).jmespath("props.pageProps.itemStruct").first or {})
            if not item_struct:
                return
            for item in self._extract_tiktok_items({"itemList": [item_struct]}):
                self._save_media_item(session, item)
        except Exception as e:
            session.warn("Failed parsing embed data: {err}", err=e)

    def _download_profile(self, session: Session, username: str) -> None:
        """
        Open the user's profile in the browser, wait for a person to solve a captcha, read
        /api/post/item_list and /api/user/story/, and queue clips and photo carousels.
        """
        captured = [0]
        has_more = [True]
        # Posts found only as links on the page, when the API was walled; resolved through the embed endpoint.
        linked_ids: List[str] = []

        def on_items(session: Session, data: Any, response: Any) -> None:
            story = "/api/user/story" in response.url
            if "/api/post/item_list" in response.url and "hasMore" in data:
                has_more[0] = bool(data.get("hasMore"))
            parsed = self._extract_tiktok_items(data, is_story=story)
            captured[0] += len(parsed)
            for item in parsed:
                self._save_media_item(session, item)

        session.on_response(is_feed_api, on_items)
        session.info("Navigating to https://www.tiktok.com/@{username}...", username=username)
        try:
            if session.goto(f"https://www.tiktok.com/@{username}"):
                self._scroll_profile(session, count=lambda: captured[0], more=lambda: has_more[0])
                # Fallback: Also collect any video/photo links visible on page if API was walled
                if not captured[0]:
                    for h in session.page.selector.css(POST_LINKS).all:
                        m = re.search(r'/(video|photo)/(\d+)', h)
                        if m:
                            linked_ids.append(m.group(2))
        except Exception as e:
            session.warn("Browser error: {err}", err=e)

        session.info("Intercepted {n} items from TikTok profile.", n=captured[0] + len(linked_ids))

        for video_id in linked_ids:
            if session.full:
                break
            # Resolve full media URLs from embed endpoint
            self._download_single_embed(session, video_id)

    def _scroll_profile(self, session: Session, *, count: Callable[[], int], more: Callable[[], bool]) -> None:
        """Wait for a person to solve a captcha, if one shows, then scroll the profile to its end."""
        if has_captcha(session):
            solved = session.handoff(until=lambda: not has_captcha(session),
                                     message="Solve the TikTok captcha in the browser window", timeout=60.0)
            if not solved:
                session.challenged()
        session.scroll(count=count, more=more, delay=2.5)

    def _save_media_item(self, session: Session, item: Dict[str, Any]) -> bool:
        """Queue a TikTok clip or photo into the appropriate subfolder with anti-403 CDN headers."""
        is_story = item.get("is_story", False)
        playlist = item.get("playlist")
        kind = item["kind"]

        if is_story:
            dest_folder, category = "stories", "stories"
        elif playlist:
            dest_folder, category = f"albums/{fmt.filename(playlist)}", "playlists"
        elif kind == "video":
            dest_folder, category = "videos", "videos"
        else:
            dest_folder, category = "images", "videos"

        ext = "mp4" if kind == "video" else "jpg"
        date_str = item.get("date") or ""
        idx = item.get("idx", 0)
        item_id = item["id"]

        filename = f"{date_str}_{item_id}_{idx}.{ext}" if (idx > 0 and date_str) else (
            f"{date_str}_{item_id}.{ext}" if date_str else (
                f"{item_id}_{idx}.{ext}" if idx > 0 else f"{item_id}.{ext}"
            )
        )

        # TikTok CDN requires Referer and session cookies (tt_chain_token) to avoid 403 Forbidden
        token = session.cookies.get("tt_chain_token", "")
        sess = session.cookies.get("sessionid", "")
        cookie_header = f"tt_chain_token={token}; sessionid={sess}" if token else f"sessionid={sess}"

        headers = {
            "Referer": "https://www.tiktok.com/",
            "User-Agent": TIKTOK_UA,
            "Cookie": cookie_header,
        }

        key = f"{item_id}_{idx}" if idx > 0 else item_id
        return session.save(item["url"], filename, category=category,
                            date=date.fromisoformat(date_str) if date_str else None, folder=dest_folder,
                            key=key, headers=headers)

    def _extract_tiktok_items(self, data: Dict[str, Any], is_story: bool = False) -> List[Dict[str, Any]]:
        """Parse itemList / storyList from TikTok API into structured media items."""
        items = []
        raw_list = selector.select(data).jmespath("itemList || storyList || story_list").all

        for raw in raw_list:
            item_id = str(raw.get("id") or raw.get("aweme_id") or "")
            if not item_id:
                continue

            create_time = raw.get("createTime") or raw.get("create_time")
            date_str = ""
            if create_time and isinstance(create_time, (int, float)) and create_time > 100000000:
                date_str = datetime.fromtimestamp(create_time).strftime("%Y-%m-%d")
            elif item_id.isdigit():
                # Derive timestamp from Snowflake ID (upper 32 bits)
                try:
                    ts = int(item_id) >> 32
                    if ts > 100000000:
                        date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                except Exception:
                    pass

            playlist_name = selector.select(raw).jmespath("playlist.name || playlist_info.name").first

            # 1. Photo mode carousel
            images = selector.select(raw).jmespath("imagePost.images").all
            if images:
                for idx, img in enumerate(images):
                    url_list = selector.select(img).jmespath(
                        "imageURL.urlList"
                        " || displayImage.urlList"
                        " || thumbnail.urlList"
                    ).all
                    if url_list:
                        items.append({
                            "id": item_id,
                            "url": url_list[0],
                            "date": date_str,
                            "kind": "image",
                            "idx": idx + 1,
                            "is_story": is_story,
                            "playlist": playlist_name,
                        })

            # 2. Video post
            else:
                video = raw.get("video", {})
                video_url = (
                    video.get("playAddr")
                    or video.get("downloadAddr")
                    or (video.get("playAddr", {}).get("urlList", [None])[0] if isinstance(video.get("playAddr"), dict) else None)
                    or (video.get("downloadAddr", {}).get("urlList", [None])[0] if isinstance(video.get("downloadAddr"), dict) else None)
                )

                # Check bitrateInfo
                if not video_url and video.get("bitrateInfo"):
                    bitrates = video["bitrateInfo"]
                    if isinstance(bitrates, list) and bitrates:
                        # Pick highest bitrate
                        best = max(bitrates, key=lambda b: b.get("Bitrate", 0))
                        play_addr = best.get("PlayAddr", {})
                        if isinstance(play_addr, dict):
                            url_list = play_addr.get("UrlList", [])
                            if url_list:
                                video_url = url_list[0]

                if video_url:
                    items.append({
                        "id": item_id,
                        "url": video_url,
                        "date": date_str,
                        "kind": "video",
                        "idx": 0,
                        "is_story": is_story,
                        "playlist": playlist_name,
                    })

        return items
