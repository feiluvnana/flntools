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

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx
from playwright.sync_api import sync_playwright

from flnd.cookies import cookies_to_dict, load_cookies, prompt_missing_cookies
from flnd.sources.base import BaseSource
from flnd.target import Target
from flnd.utils import download_file, sanitize_name, scroll_page_to_end

TIKTOK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class TikSource(BaseSource):
    """TikTok autonomous scraper supporting profile videos, photo mode, stories, and albums."""

    code = "tik"

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

        if "sessionid" not in cookie_map and "sid_tt" not in cookie_map:
            prompt_missing_cookies(self.code)
            print("  [warn] Running without session cookies; TikTok rate-limits public requests aggressively.")

        target_dir = download_dir / "tik" / username_or_id
        target_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n[tik] Starting download for: {username_or_id}")
        print(f"      Destination: {target_dir}")
        print(f"      Browser mode: {'headless' if headless else 'non-headless (visible)'}")

        # Check if target is a single numeric video ID
        if username_or_id.isdigit() and len(username_or_id) > 15:
            return self._download_single_embed(username_or_id, target_dir, cookie_map)

        # Scrape profile media
        return self._download_profile(username_or_id, target_dir, cookies_list, cookie_map, limit, headless)

    def _download_single_embed(self, video_id: str, target_dir: Path, cookie_map: Dict[str, str]) -> int:
        """Download a single video or photo post via TikTok public embed endpoint."""
        embed_url = f"https://www.tiktok.com/embed/v2/{video_id}"
        headers = {"User-Agent": TIKTOK_UA}

        with httpx.Client(headers=headers, timeout=20.0) as client:
            resp = client.get(embed_url)
            if resp.status_code != 200:
                print(f"  [fail] HTTP {resp.status_code} on embed endpoint for {video_id}")
                return 0

            match = re.search(r'id="__NEXT_DATA__".*?>(.*?)</script>', resp.text, re.DOTALL)
            if not match:
                print(f"  [warn] Could not extract item data from embed HTML for {video_id}")
                return 0

            try:
                data = json.loads(match.group(1))
                item_struct = data.get("props", {}).get("pageProps", {}).get("itemStruct", {})
                if not item_struct:
                    return 0
                items = self._extract_tiktok_items({"itemList": [item_struct]})
                saved = 0
                for item in items:
                    saved += self._save_media_item(item, target_dir, cookie_map, client=client)
                return saved
            except Exception as e:
                print(f"  [error] Failed parsing embed data: {e}")
                return 0

    def _download_profile(
        self,
        username: str,
        target_dir: Path,
        cookies_list: List[Dict[str, Any]],
        cookie_map: Dict[str, str],
        limit: Optional[int],
        headless: bool,
    ) -> int:
        """
        Open user profile in non-headless Playwright browser, detect/wait for captcha,
        intercept /api/post/item_list and /api/user/story/, and download clips and photo carousels.
        """
        captured_items: List[Dict[str, Any]] = []

        print(f"  [browser] Navigating to https://www.tiktok.com/@{username}...")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled", "--disable-infobars", "--no-sandbox"],
            )
            context = browser.new_context(
                user_agent=TIKTOK_UA,
                viewport={"width": 1280, "height": 900},
            )

            # Inject session cookies into context
            if cookies_list:
                formatted_cookies = []
                for c in cookies_list:
                    fc = {
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c.get("domain", ".tiktok.com"),
                        "path": c.get("path", "/"),
                    }
                    if not fc["domain"].startswith("."):
                        fc["domain"] = "." + fc["domain"]
                    formatted_cookies.append(fc)
                try:
                    context.add_cookies(formatted_cookies)
                except Exception as e:
                    print(f"  [warn] Cookie injection error: {e}")

            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            has_more = [True]

            # Intercept API responses
            def on_response(response):
                url = response.url
                if "/api/post/item_list" in url or "/api/user/story" in url:
                    try:
                        b = response.body()
                        if b and len(b) > 0:
                            data = json.loads(b)
                            story = "/api/user/story" in url
                            if "/api/post/item_list" in url and "hasMore" in data:
                                has_more[0] = bool(data.get("hasMore"))
                            parsed = self._extract_tiktok_items(data, is_story=story)
                            captured_items.extend(parsed)
                    except Exception:
                        pass

            page.on("response", on_response)

            try:
                page.goto(f"https://www.tiktok.com/@{username}", wait_until="load", timeout=30000)
                page.wait_for_timeout(3500)

                # Captcha verification detection
                captcha_detector = """() => {
                    return !!document.querySelector('#captcha-verify-container-main-page, #captcha_container, [id*="captcha-verify"], [class*="captcha_verify"], [class*="captcha-verify"]')
                        || /Drag the slider|Drag the puzzle|Verify to continue|Select 2 objects/i.test(document.body ? document.body.innerText : '');
                }"""

                if page.evaluate(captcha_detector):
                    print("  [tik] TikTok captcha detected ('Drag the slider to fit the puzzle').")
                    print("        Please complete the puzzle in the visible browser window...")
                    # Wait up to 60 seconds for user to solve
                    for _ in range(60):
                        page.wait_for_timeout(1000)
                        if not page.evaluate(captcha_detector):
                            print("  [tik] Captcha solved! Continuing download...")
                            page.wait_for_timeout(3000)
                            break

                def tiktok_scroll(p):
                    p.evaluate("""() => {
                        const posts = document.querySelectorAll('a[href*="/video/"], a[href*="/photo/"]');
                        if (posts.length > 0) {
                            posts[posts.length - 1].scrollIntoView({block: 'end'});
                        }
                        window.scrollBy(0, 800);
                        window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
                        window.dispatchEvent(new Event('scroll'));
                    }""")

                # Continuously scroll to the end of the profile
                scroll_page_to_end(
                    page,
                    get_count=lambda: len(captured_items),
                    limit=limit,
                    max_idle_rounds=4,
                    step_delay_ms=2500,
                    check_has_more=lambda: has_more[0],
                    custom_scroll_action=tiktok_scroll,
                )

                # Fallback: Also collect any video/photo links visible on page if API was walled
                if not captured_items:
                    hrefs = page.evaluate("""() => {
                        return Array.from(document.querySelectorAll('a[href*="/video/"], a[href*="/photo/"]')).map(a => a.href);
                    }""")
                    for h in hrefs:
                        m = re.search(r'/(video|photo)/(\d+)', h)
                        if m:
                            kind = "video" if m.group(1) == "video" else "image"
                            vid = m.group(2)
                            # Timestamp from Snowflake ID
                            date_str = ""
                            try:
                                ts = int(vid) >> 32
                                if ts > 100000000:
                                    date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                            except Exception:
                                pass
                            captured_items.append({
                                "id": vid,
                                "url": h,
                                "kind": kind,
                                "date": date_str,
                                "is_story": False,
                                "needs_embed": True,
                            })

            except Exception as e:
                print(f"  [warn] Browser error: {e}")

            browser.close()

        print(f"  [browser] Intercepted {len(captured_items)} items from TikTok profile.")

        # Download media to platform-optimized layout
        saved_count = 0
        with httpx.Client(timeout=30.0) as client:
            for item in captured_items:
                if item.get("needs_embed"):
                    # Resolve full media URLs from embed endpoint
                    sub_count = self._download_single_embed(item["id"], target_dir, cookie_map)
                    saved_count += sub_count
                else:
                    if self._save_media_item(item, target_dir, cookie_map, client=client):
                        saved_count += 1

                if limit and saved_count >= limit:
                    print(f"\n[tik] Reached limit of {limit} downloads.")
                    break

        return saved_count

    def _save_media_item(
        self,
        item: Dict[str, Any],
        target_dir: Path,
        cookie_map: Dict[str, str],
        client: httpx.Client,
    ) -> bool:
        """Download TikTok clip or photo into appropriate subfolder with anti-403 CDN headers."""
        is_story = item.get("is_story", False)
        playlist = item.get("playlist")
        kind = item["kind"]

        if is_story:
            dest_folder = target_dir / "stories"
        elif playlist:
            dest_folder = target_dir / "albums" / sanitize_name(playlist)
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

        return download_file(item["url"], dest_path, headers=headers, client=client)

    def _extract_tiktok_items(self, data: Dict[str, Any], is_story: bool = False) -> List[Dict[str, Any]]:
        """Parse itemList / storyList from TikTok API into structured media items."""
        items = []
        raw_list = data.get("itemList", []) or data.get("storyList", []) or data.get("story_list", [])

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

            playlist_name = raw.get("playlist", {}).get("name") or raw.get("playlist_info", {}).get("name")

            # 1. Photo mode carousel
            images = raw.get("imagePost", {}).get("images", [])
            if images:
                for idx, img in enumerate(images):
                    url_list = (
                        img.get("imageURL", {}).get("urlList", [])
                        or img.get("displayImage", {}).get("urlList", [])
                        or img.get("thumbnail", {}).get("urlList", [])
                    )
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
