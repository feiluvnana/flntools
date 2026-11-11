"""
Facebook source extractor for flnd.

Features:
  - Reads cookies (c_user, xs, fr, datr) from .flnd/cookies/fbk.txt
  - Navigates profile tabs in non-headless browser:
      * Posts / Timeline: https://www.facebook.com/<user>/ -> posts/
      * Photos: https://www.facebook.com/<user>/photos -> photos/
      * Tagged / Photos of: https://www.facebook.com/<user>/photos_of -> tagged/
      * Albums: https://www.facebook.com/<user>/photos_albums -> albums/<album_title>/
      * Reels: https://www.facebook.com/<user>/reels -> reels/
      * Videos: https://www.facebook.com/<user>/videos -> videos/
  - Real creation date parsing from Unix timestamps (creation_time, publish_time)
  - HD progressive video selection

Folder layout:
  .flnd/downloads/fbk/<username_or_id>/
  ├── posts/<YYYY-MM-DD>_<post_id>.jpg (or .mp4)
  ├── photos/<YYYY-MM-DD>_<photo_id>.jpg
  ├── tagged/<YYYY-MM-DD>_<photo_id>.jpg
  ├── reels/<YYYY-MM-DD>_<reel_id>.mp4
  ├── videos/<YYYY-MM-DD>_<video_id>.mp4
  ├── albums/<album_title>/<YYYY-MM-DD>_<photo_id>.jpg
  └── stories/<YYYY-MM-DD>_<story_id>.jpg (or .mp4)
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import httpx
from playwright.sync_api import sync_playwright

from flnd.cookies import ask_for_cookies, cookies_to_dict, load_cookies
from flnd.sources.base import BaseSource
from flnd.target import Target
from flnd.utils import download_file, sanitize_name, scroll_page_to_end, show_steps
from shared import log

FB_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class FbkSource(BaseSource):
    """Facebook autonomous scraper supporting photos, albums, reels, videos, stories, and tagged media."""

    code = "fbk"
    name = "Facebook"
    domains = ("facebook.com",)

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

        if ("c_user" not in cookie_map or "xs" not in cookie_map) and ask_for_cookies(self.code):
            cookies_list = load_cookies(self.code)
            cookie_map = cookies_to_dict(cookies_list)
        if "c_user" not in cookie_map or "xs" not in cookie_map:
            log.warn("Running without c_user/xs cookies; Facebook blocks unauthenticated profile browsing.")

        target_dir = download_dir / "fbk" / username_or_id
        target_dir.mkdir(parents=True, exist_ok=True)

        log.info("Starting download for: {target}", target=username_or_id)
        log.info("Destination: {path}", path=target_dir)
        log.info("Browser mode: {mode}", mode="headless" if headless else "visible")

        total_downloaded = 0
        with httpx.Client(timeout=30.0) as http_client:
            total_downloaded = self._download_via_browser(
                username_or_id, target_dir, cookies_list, limit, headless, http_client
            )

        log.ok("Finished {target}: {n} files", target=username_or_id, n=total_downloaded)
        return total_downloaded

    def _download_via_browser(
        self,
        target_name: str,
        target_dir: Path,
        cookies_list: List[Dict[str, Any]],
        limit: Optional[int],
        headless: bool,
        client: httpx.Client,
    ) -> int:
        """
        Launch browser to navigate Facebook profile tabs:
          - posts (timeline)
          - photos (uploads)
          - photos_of (tagged)
          - photos_albums (albums)
          - reels
          - videos
        Intercept Comet GraphQL responses and parse real Unix timestamps.
        """
        saved_count = 0

        # Tabs to navigate in order: (folder_name, relative_url_path)
        tabs = [
            ("posts", ""),
            ("photos", "photos"),
            ("tagged", "photos_of"),
            ("albums", "photos_albums"),
            ("reels", "reels"),
            ("videos", "videos"),
        ]

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled", "--disable-infobars"],
            )
            context = browser.new_context(
                user_agent=FB_DESKTOP_UA,
                viewport={"width": 1280, "height": 900},
            )

            # Inject session cookies into context
            if cookies_list:
                formatted_cookies = []
                for c in cookies_list:
                    fc = {
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c.get("domain", ".facebook.com"),
                        "path": c.get("path", "/"),
                    }
                    if not fc["domain"].startswith("."):
                        fc["domain"] = "." + fc["domain"]
                    formatted_cookies.append(fc)
                try:
                    context.add_cookies(formatted_cookies)
                except Exception as e:
                    log.warn("Cookie injection error: {err}", err=e)

            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            # Store media items captured per tab
            tab_media: Dict[str, List[Dict[str, Any]]] = {name: [] for name, _ in tabs}
            current_tab = "posts"

            def on_response(response):
                url = response.url
                if "graphql" in url or "/api/graphql" in url:
                    try:
                        text = response.text()
                        for line in text.splitlines():
                            line = line.strip()
                            if line.startswith("{") and line.endswith("}"):
                                try:
                                    data = json.loads(line)
                                    items = self._parse_facebook_graphql(data, default_category=current_tab)
                                    if items:
                                        tab_media[current_tab].extend(items)
                                except Exception:
                                    pass
                    except Exception:
                        pass

            page.on("response", on_response)

            for tab_name, tab_path in tabs:
                if limit and saved_count >= limit:
                    break

                current_tab = tab_name
                tab_url = f"https://www.facebook.com/{target_name}/{tab_path}".rstrip("/")
                log.info("Navigating tab {tab}: {url}", tab=tab_name, url=tab_url)

                try:
                    page.goto(tab_url, wait_until="load", timeout=25000)
                    page.wait_for_timeout(3000)

                    # Continuously scroll down to the end of the feed/tab
                    with log.progress(f"scroll {tab_name}", show=show_steps) as bar:
                        scroll_page_to_end(
                            page,
                            get_count=lambda: saved_count + len(tab_media[tab_name]),
                            limit=limit,
                            max_idle_rounds=4,
                            step_delay_ms=2000,
                            on_step=bar.update,
                        )

                except Exception as e:
                    log.warn("Navigation error on {url}: {err}", url=tab_url, err=e)

                # Download media captured for this tab
                items = tab_media[tab_name]
                log.info("{tab}: intercepted {n} media items.", tab=tab_name, n=len(items))

                for item in items:
                    category = item.get("category", tab_name)
                    album_title = item.get("album")
                    date_str = item.get("date", "").strip()
                    item_id = item["id"]
                    kind = item["kind"]
                    ext = "mp4" if kind == "video" else "jpg"

                    # Determine destination subfolder
                    if album_title:
                        dest_folder = target_dir / "albums" / sanitize_name(album_title)
                    elif category == "reels":
                        dest_folder = target_dir / "reels"
                    elif category == "videos" or kind == "video":
                        dest_folder = target_dir / "videos"
                    elif category == "tagged":
                        dest_folder = target_dir / "tagged"
                    elif category == "photos":
                        dest_folder = target_dir / "photos"
                    else:
                        dest_folder = target_dir / "posts"

                    # Do NOT default to today's date when date is unknown; format cleanly
                    filename = f"{date_str}_{item_id}.{ext}" if date_str else f"{item_id}.{ext}"
                    dest_path = dest_folder / filename

                    headers = {"Referer": f"https://www.facebook.com/{target_name}"}
                    if download_file(item["url"], dest_path, headers=headers, client=client):
                        saved_count += 1
                        if limit and saved_count >= limit:
                            log.info("Reached limit of {limit} downloads.", limit=limit)
                            browser.close()
                            return saved_count

            browser.close()

        return saved_count

    def _parse_facebook_graphql(self, data: Dict[str, Any], default_category: str = "posts") -> List[Dict[str, Any]]:
        """
        Recursively extract photos, progressive videos, reels, and albums from Facebook Comet GraphQL.
        Extracts real Unix timestamps (creation_time, publish_time, etc.).
        """
        extracted = []
        seen_ids = set()

        def extract_timestamp(obj: Dict[str, Any]) -> Optional[str]:
            """Extract Unix timestamp and format as YYYY-MM-DD."""
            for key in ["creation_time", "publish_time", "post_timestamp", "timestamp", "created_time"]:
                ts = obj.get(key)
                if isinstance(ts, (int, float)) and ts > 100000000:
                    try:
                        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                    except Exception:
                        pass
                elif isinstance(ts, str) and ts.isdigit() and len(ts) >= 9:
                    try:
                        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
                    except Exception:
                        pass
            return None

        def walk(node: Any, inherited_date: Optional[str] = None):
            if isinstance(node, dict):
                # Update current date if node defines its own creation_time
                node_date = extract_timestamp(node) or inherited_date
                typename = node.get("__typename", "")

                # 1. Video extraction (HD progressive URL)
                if "Video" in typename or "Reel" in typename or "progressive_urls" in node:
                    video_id = str(node.get("id") or node.get("video_id") or "")
                    if video_id and video_id not in seen_ids:
                        progressive = node.get("progressive_urls", [])
                        best_url = None
                        for p in progressive:
                            if p.get("metadata", {}).get("quality") == "HD":
                                best_url = p.get("progressive_url")
                                break
                        if not best_url and progressive:
                            best_url = progressive[0].get("progressive_url")
                        if not best_url:
                            best_url = (
                                node.get("browser_native_hd_url")
                                or node.get("playable_url_quality_hd")
                                or node.get("browser_native_sd_url")
                                or node.get("playable_url")
                            )

                        if best_url:
                            seen_ids.add(video_id)
                            category = "reels" if ("Reel" in typename or default_category == "reels") else "videos"
                            extracted.append({
                                "id": video_id,
                                "url": best_url,
                                "kind": "video",
                                "category": category,
                                "date": node_date or "",
                            })

                # 2. Photo extraction
                elif (
                    "Photo" in typename
                    or "large_share_image" in node
                    or "viewer_image" in node
                    or "photo_image" in node
                    or ("image" in node and isinstance(node["image"], dict) and "uri" in node["image"])
                ):
                    photo_id = str(node.get("id") or node.get("photo_id") or node.get("legacy_photo_id") or "")
                    img_url = (
                        node.get("viewer_image", {}).get("uri")
                        or node.get("large_share_image", {}).get("uri")
                        or node.get("photo_image", {}).get("uri")
                        or (node.get("image", {}).get("uri") if isinstance(node.get("image"), dict) else None)
                    )
                    if photo_id and img_url and photo_id not in seen_ids:
                        seen_ids.add(photo_id)
                        album_name = node.get("album", {}).get("name")
                        extracted.append({
                            "id": photo_id,
                            "url": img_url,
                            "kind": "image",
                            "category": default_category,
                            "album": album_name,
                            "date": node_date or "",
                        })

                # Recurse children with inherited date
                for v in node.values():
                    walk(v, inherited_date=node_date)
            elif isinstance(node, list):
                for item in node:
                    walk(item, inherited_date=inherited_date)

        walk(data)
        return extracted
