"""
TikTok source extractor for flnd.

Features:
  - Reads cookies (sessionid, sid_tt, uid_tt, tt_chain_token) from .flnd/cookies/tik.txt
  - Navigates profile in non-headless browser with captcha detection & prompt
  - Intercepts /api/post/item_list and /api/user/story
  - Downloads full MP4 video clips into videos/
  - Downloads full photo-mode carousels into images/
  - Downloads active stories into stories/
  - Downloads playlists/series into albums/<playlist_title>/
  - Resolves Snowflake timestamp (BigInt(id) >> 32) if createTime is missing
  - Fixes CDN 403 Forbidden by passing tt_chain_token + sessionid + Referer

Folder layout:
  .flnd/downloads/tik/<username>/
  ├── videos/<YYYY-MM-DD>_<video_id>.mp4
  ├── images/<YYYY-MM-DD>_<post_id>_<idx>.jpg
  ├── stories/<YYYY-MM-DD>_<story_id>.mp4 (or .jpg)
  └── albums/<playlist_title>/<YYYY-MM-DD>_<video_id>.mp4
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


from flnd.cookies import ask_for_cookies, cookies_to_dict, load_cookies
from flnd.sources.base import BaseSource
from flnd.target import Target
from flnd.utils import Downloads, open_page, scroll
from shared import fmt, http, log, selector

TIKTOK_UA = http.agents.chrome

# Captcha verification detection
CAPTCHA_DETECTOR = """() => {
    return !!document.querySelector('#captcha-verify-container-main-page, #captcha_container, [id*="captcha-verify"], [class*="captcha_verify"], [class*="captcha-verify"]')
        || /Drag the slider|Drag the puzzle|Verify to continue|Select 2 objects/i.test(document.body ? document.body.innerText : '');
}"""

TIKTOK_SCROLL = """() => {
    const posts = document.querySelectorAll('a[href*="/video/"], a[href*="/photo/"]');
    if (posts.length > 0) {
        posts[posts.length - 1].scrollIntoView({block: 'end'});
    }
    window.scrollBy(0, 800);
    window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
    window.dispatchEvent(new Event('scroll'));
}"""

POST_LINKS = """() => {
    return Array.from(document.querySelectorAll('a[href*="/video/"], a[href*="/photo/"]')).map(a => a.href);
}"""


def is_feed_api(response: Any) -> bool:
    return "/api/post/item_list" in response.url or "/api/user/story" in response.url


class TikSource(BaseSource):
    """TikTok autonomous scraper supporting profile videos, photo mode, stories, and albums."""

    code = "tik"
    name = "TikTok"
    domains = ("tiktok.com",)

    def download(
        self,
        target: Target,
        download_dir: Path,
        limit: Optional[int] = None,
        headless: bool = False,
    ) -> int:
        username_or_id = target.value
        cookies_list = load_cookies(self.code)
        cookie_map = cookies_to_dict(cookies_list)

        if "sessionid" not in cookie_map and "sid_tt" not in cookie_map and ask_for_cookies(self.code):
            cookies_list = load_cookies(self.code)
            cookie_map = cookies_to_dict(cookies_list)
        if "sessionid" not in cookie_map and "sid_tt" not in cookie_map:
            log.warn("Running without session cookies; TikTok rate-limits public requests aggressively.")

        target_dir = download_dir / "tik" / username_or_id
        target_dir.mkdir(parents=True, exist_ok=True)

        log.info("Starting download for: {target}", target=username_or_id)
        log.info("Destination: {path}", path=target_dir)
        log.info("Browser mode: {mode}", mode="headless" if headless else "visible")

        with http.Client(user_agent=TIKTOK_UA) as client, Downloads(self.code, target, download_dir, limit) as files:
            # Check if target is a single numeric video ID
            if username_or_id.isdigit() and len(username_or_id) > 15:
                self._download_single_embed(username_or_id, target_dir, cookie_map, client, files)
            else:
                # Scrape profile media
                self._download_profile(username_or_id, target_dir, cookies_list, cookie_map, headless, client, files)
        return files.saved

    def _download_single_embed(
        self, video_id: str, target_dir: Path, cookie_map: Dict[str, str], client: http.Client, files: Downloads
    ) -> None:
        """Queue a single video or photo post via TikTok public embed endpoint."""
        embed_url = f"https://www.tiktok.com/embed/v2/{video_id}"

        resp = client.get(embed_url, timeout=20.0, follow_redirects=False)
        if resp.status_code != 200:
            log.error("HTTP {status} on embed endpoint for {id}", status=resp.status_code, id=video_id)
            return

        try:
            data = selector.select(resp).script("__NEXT_DATA__").first
            if data is None:
                log.warn("Could not extract item data from embed HTML for {id}", id=video_id)
                return
            item_struct = (selector.select(data).jmespath("props.pageProps.itemStruct").first or {})
            if not item_struct:
                return
            for item in self._extract_tiktok_items({"itemList": [item_struct]}):
                self._save_media_item(item, target_dir, cookie_map, client, files)
        except Exception as e:
            log.error("Failed parsing embed data: {err}", err=e)

    def _download_profile(
        self,
        username: str,
        target_dir: Path,
        cookies_list: List[Dict[str, Any]],
        cookie_map: Dict[str, str],
        headless: bool,
        client: http.Client,
        files: Downloads,
    ) -> None:
        """
        Open user profile in non-headless Playwright browser, detect/wait for captcha,
        intercept /api/post/item_list and /api/user/story/, and queue clips and photo carousels.
        """
        captured = [0]
        has_more = [True]
        # Posts found only as links on the page, when the API was walled; resolved through the embed endpoint.
        linked_ids: List[str] = []

        log.info("Navigating to https://www.tiktok.com/@{username}...", username=username)

        def on_items(data: Any, response: Any) -> None:
            story = "/api/user/story" in response.url
            if "/api/post/item_list" in response.url and "hasMore" in data:
                has_more[0] = bool(data.get("hasMore"))
            parsed = self._extract_tiktok_items(data, is_story=story)
            captured[0] += len(parsed)
            for item in parsed:
                self._save_media_item(item, target_dir, cookie_map, client, files)

        with open_page(headless=headless, cookies=cookies_list, args=["--no-sandbox"]) as page:
            page.on_response(is_feed_api, on_items)

            try:
                page.raw.goto(f"https://www.tiktok.com/@{username}", wait_until="load", timeout=30000)
                page.raw.wait_for_timeout(3500)

                if page.raw.evaluate(CAPTCHA_DETECTOR):
                    solved = page.handoff(
                        until=lambda: not page.raw.evaluate(CAPTCHA_DETECTOR),
                        message="Solve the TikTok captcha in the browser window",
                        timeout=60.0,
                    )
                    if solved:
                        page.raw.wait_for_timeout(3000)

                # Continuously scroll to the end of the profile
                scroll(
                    page,
                    "profile",
                    count=lambda: captured[0],
                    more=lambda: has_more[0] and not files.full,
                    delay=2.5,
                    step=lambda p: p.evaluate(TIKTOK_SCROLL),
                )

                # Fallback: Also collect any video/photo links visible on page if API was walled
                if not captured[0]:
                    for h in page.raw.evaluate(POST_LINKS):
                        m = re.search(r'/(video|photo)/(\d+)', h)
                        if m:
                            linked_ids.append(m.group(2))

            except Exception as e:
                log.warn("Browser error: {err}", err=e)

        log.info("Intercepted {n} items from TikTok profile.", n=captured[0] + len(linked_ids))

        for video_id in linked_ids:
            if files.full:
                break
            # Resolve full media URLs from embed endpoint
            self._download_single_embed(video_id, target_dir, cookie_map, client, files)

    def _save_media_item(
        self,
        item: Dict[str, Any],
        target_dir: Path,
        cookie_map: Dict[str, str],
        client: http.Client,
        files: Downloads,
    ) -> bool:
        """Queue a TikTok clip or photo into the appropriate subfolder with anti-403 CDN headers."""
        is_story = item.get("is_story", False)
        playlist = item.get("playlist")
        kind = item["kind"]

        if is_story:
            dest_folder = target_dir / "stories"
        elif playlist:
            dest_folder = target_dir / "albums" / fmt.filename(playlist)
        elif kind == "video":
            dest_folder = target_dir / "videos"
        else:
            dest_folder = target_dir / "images"

        ext = "mp4" if kind == "video" else "jpg"
        date_str = item.get("date") or ""
        idx = item.get("idx", 0)
        item_id = item["id"]

        filename = f"{date_str}_{item_id}_{idx}.{ext}" if (idx > 0 and date_str) else (
            f"{date_str}_{item_id}.{ext}" if date_str else (
                f"{item_id}_{idx}.{ext}" if idx > 0 else f"{item_id}.{ext}"
            )
        )
        dest_path = dest_folder / filename

        # TikTok CDN requires Referer and session cookies (tt_chain_token) to avoid 403 Forbidden
        token = cookie_map.get("tt_chain_token", "")
        sess = cookie_map.get("sessionid", "")
        cookie_header = f"tt_chain_token={token}; sessionid={sess}" if token else f"sessionid={sess}"

        headers = {
            "Referer": "https://www.tiktok.com/",
            "User-Agent": TIKTOK_UA,
            "Cookie": cookie_header,
        }

        key = f"{item_id}_{idx}" if idx > 0 else item_id
        return files.add(client, item["url"], dest_path, key=key, headers=headers)

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
