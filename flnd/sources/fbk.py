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

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


from flnd.cookies import ask_for_cookies, cookies_to_dict, load_cookies
from flnd.sources.base import BaseSource
from flnd.target import Target
from flnd.utils import Downloads, open_page, scroll
from shared import fmt, http, log, selector


# Tabs to navigate in order: (folder_name, relative_url_path)
TABS = [
    ("posts", ""),
    ("photos", "photos"),
    ("tagged", "photos_of"),
    ("albums", "photos_albums"),
    ("reels", "reels"),
    ("videos", "videos"),
]


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

        with http.Client() as client, Downloads(self.code, target, download_dir, limit) as files:
            self._download_via_browser(username_or_id, target_dir, cookies_list, headless, client, files)

        log.ok("Finished {target}: {n} files", target=username_or_id, n=files.saved)
        return files.saved

    def _download_via_browser(
        self,
        target_name: str,
        target_dir: Path,
        cookies_list: List[Dict[str, Any]],
        headless: bool,
        client: http.Client,
        files: Downloads,
    ) -> None:
        """
        Launch browser to navigate Facebook profile tabs:
          - posts (timeline)
          - photos (uploads)
          - photos_of (tagged)
          - photos_albums (albums)
          - reels
          - videos
        Intercept Comet GraphQL responses, parse real Unix timestamps, and queue each file as it is found.
        """
        found = {name: 0 for name, _ in TABS}
        current = {"tab": "posts"}
        headers = {"Referer": f"https://www.facebook.com/{target_name}"}

        def on_graphql(data: Any, response: Any) -> None:
            # Facebook sends one JSON object per line; other lines were never read.
            if not isinstance(data, dict):
                return
            tab = current["tab"]
            items = self._parse_facebook_graphql(data, default_category=tab)
            found[tab] += len(items)
            for item in items:
                self._queue_item(item, tab, target_dir, client, files, headers)

        with open_page(headless=headless, cookies=cookies_list) as page:
            page.on_response("graphql", on_graphql, lines=True)

            for tab_name, tab_path in TABS:
                if files.full:
                    break

                current["tab"] = tab_name
                tab_url = f"https://www.facebook.com/{target_name}/{tab_path}".rstrip("/")
                log.info("Navigating tab {tab}: {url}", tab=tab_name, url=tab_url)

                try:
                    page.raw.goto(tab_url, wait_until="load", timeout=25000)
                    page.raw.wait_for_timeout(3000)

                    # Continuously scroll down to the end of the feed/tab
                    scroll(page, tab_name, count=lambda: found[tab_name], more=lambda: not files.full)

                except Exception as e:
                    log.warn("Navigation error on {url}: {err}", url=tab_url, err=e)

                log.info("{tab}: intercepted {n} media items.", tab=tab_name, n=found[tab_name])

    def _queue_item(
        self,
        item: Dict[str, Any],
        tab_name: str,
        target_dir: Path,
        client: http.Client,
        files: Downloads,
        headers: Dict[str, str],
    ) -> None:
        category = item.get("category", tab_name)
        album_title = item.get("album")
        date_str = item.get("date", "").strip()
        item_id = item["id"]
        kind = item["kind"]
        ext = "mp4" if kind == "video" else "jpg"

        # Determine destination subfolder
        if album_title:
            dest_folder = target_dir / "albums" / fmt.filename(album_title)
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
        files.add(client, item["url"], dest_folder / filename, key=item_id, headers=headers)

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
                            if selector.select(p).jmespath("metadata.quality").first == "HD":
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
                    img_url = selector.select(node).jmespath(
                        "viewer_image.uri"
                        " || large_share_image.uri"
                        " || photo_image.uri"
                        " || image.uri"
                    ).first
                    if photo_id and img_url and photo_id not in seen_ids:
                        seen_ids.add(photo_id)
                        album_name = selector.select(node).jmespath("album.name").first
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
