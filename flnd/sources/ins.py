"""
Instagram source extractor for flnd.

Features:
  - Reads cookies (sessionid, ds_user_id, csrftoken) from .flnd/cookies/ins.txt
  - Navigates profile tabs in browser (non-headless by default):
      * Posts: https://www.instagram.com/<user>/ -> posts/
      * Reels: https://www.instagram.com/<user>/reels/ -> reels/
      * Tagged: https://www.instagram.com/<user>/tagged/ -> tagged/
      * Reposts: https://www.instagram.com/<user>/reposts/ -> reposts/
      * Stories: https://www.instagram.com/stories/<user>/ -> stories/
      * Highlights: https://www.instagram.com/stories/highlights/<id>/ -> highlights/<title>/
  - Supports multi-photo carousel slides, reels, and stories
  - Uses proper headers (desktop & mobile User-Agent, referer, csrf)

Folder layout:
  .flnd/downloads/ins/<username>/
  ├── posts/<YYYY-MM-DD>_<post_id>_<idx>.jpg (or .mp4)
  ├── reels/<YYYY-MM-DD>_<reel_id>.mp4
  ├── tagged/<YYYY-MM-DD>_<post_id>_<idx>.jpg (or .mp4)
  ├── reposts/<YYYY-MM-DD>_<post_id>_<idx>.jpg (or .mp4)
  ├── stories/<YYYY-MM-DD>_<story_id>.jpg (or .mp4)
  └── highlights/<title>/<YYYY-MM-DD>_<story_id>.jpg (or .mp4)
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from flnd.cookies import ask_for_cookies, cookies_to_dict, load_cookies
from flnd.sources.base import BaseSource
from flnd.target import Target
from flnd.utils import Downloads, scroll
from shared import browser, http, log, path, scrape

# Mobile Android User-Agent: Instagram story tray API refuses desktop user-agents
INSTAGRAM_MOBILE_UA = (
    "Instagram 219.0.0.12.117 Android (33/13; 420dpi; 1080x2400; samsung; SM-G991B; o1s; exynos2100; en_US; 346132297)"
)

INSTAGRAM_DESKTOP_UA = http.agents.chrome

# Tabs to navigate in order: (folder_name, relative_path)
TABS = [
    ("posts", ""),
    ("reels", "reels/"),
    ("tagged", "tagged/"),
    ("reposts", "reposts/"),
]


def is_profile_api(response: Any) -> bool:
    return "graphql" in response.url or "/api/v1/" in response.url


class InsSource(BaseSource):
    """Instagram autonomous scraper supporting posts, reels, tagged, reposts, stories, and highlights."""

    code = "ins"
    name = "Instagram"
    domains = ("instagram.com",)

    def download(
        self,
        target: Target,
        download_dir: Path,
        limit: Optional[int] = None,
        headless: bool = False,
    ) -> int:
        username = target.value
        cookies_list = load_cookies(self.code)
        cookie_map = cookies_to_dict(cookies_list)

        if "sessionid" not in cookie_map and ask_for_cookies(self.code):
            cookies_list = load_cookies(self.code)
            cookie_map = cookies_to_dict(cookies_list)
        if "sessionid" not in cookie_map:
            log.warn("Running without sessionid; Instagram will likely wall requests.")

        target_dir = path.mkdir(download_dir / "ins" / username)

        log.info("Starting download for: @{username}", username=username)
        log.info("Destination: {path}", path=target_dir)
        log.info("Browser mode: {mode}", mode="headless" if headless else "visible")

        with http.Client() as client, Downloads(self.code, target, download_dir, limit) as files:
            # 1. Fetch active 24h stories via mobile API tray
            user_id = cookie_map.get("ds_user_id")
            if user_id:
                log.info("Checking active 24h stories tray...")
                self._download_active_stories(user_id, target_dir, cookie_map, client, files)

            # 2. Launch browser to navigate profile tabs: posts, reels, tagged, reposts, highlights
            if not files.full:
                self._download_via_browser(username, target_dir, cookies_list, headless, client, files)

        log.ok("Finished @{username}: {n} files", username=username, n=files.saved)
        return files.saved

    def _download_active_stories(
        self,
        user_id: str,
        target_dir: Path,
        cookie_map: Dict[str, str],
        client: http.Client,
        files: Downloads,
    ) -> None:
        """Queue active user stories from Instagram's mobile reels_media API."""
        cookie_str = "; ".join([f"{k}={v}" for k, v in cookie_map.items()])
        headers = {
            "user-agent": INSTAGRAM_MOBILE_UA,
            "cookie": cookie_str,
            "x-ig-app-id": "936619743392459",
        }

        url = "https://i.instagram.com/api/v1/feed/reels_media/"
        params = {"user_ids": json.dumps([user_id]), "source": "profile"}

        try:
            data = client.json(url, headers=headers, params=params, follow_redirects=False)
            reels = data.get("reels_media", []) or list(data.get("reels", {}).values())
            stories_dir = target_dir / "stories"
            queued = 0

            for reel in reels:
                items = reel.get("items", [])
                for item in items:
                    story_id = str(item.get("id") or item.get("pk") or "").split("_")[0]
                    ts = item.get("taken_at")
                    date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else datetime.now().strftime("%Y-%m-%d")

                    # Check for video first, then highest quality image
                    if item.get("media_type") == 2 and item.get("video_versions"):
                        best_video = item["video_versions"][0]["url"]
                        dest = stories_dir / f"{date_str}_{story_id}.mp4"
                        queued += files.add(client, best_video, dest, key=story_id)
                    elif scrape.dig(item, "image_versions2.candidates"):
                        best_image = item["image_versions2"]["candidates"][0]["url"]
                        dest = stories_dir / f"{date_str}_{story_id}.jpg"
                        queued += files.add(client, best_image, dest, key=story_id)

            if queued:
                log.info("Found {n} active stories in the mobile tray.", n=queued)

        except httpx.HTTPStatusError:
            pass
        except Exception as e:
            log.warn("Could not fetch stories from mobile API: {err}", err=e)

    def _download_via_browser(
        self,
        username: str,
        target_dir: Path,
        cookies_list: List[Dict[str, Any]],
        headless: bool,
        client: http.Client,
        files: Downloads,
    ) -> None:
        """
        Use Playwright browser to navigate all user profile tabs:
          - / (Posts)
          - /reels/ (Reels)
          - /tagged/ (Tagged posts)
          - /reposts/ (Reposts, if enabled)
          - Highlights collections
        Intercepts GraphQL network responses and queues media into platform-optimized subfolders.
        """
        captured_highlights: List[Dict[str, Any]] = []
        found = {name: 0 for name, _ in TABS}
        # The tab whose media is being collected; None once the tabs are done.
        current: Dict[str, Optional[str]] = {"tab": "posts"}
        headers = {"Referer": f"https://www.instagram.com/{username}/"}

        def on_profile(response: Any, data: Any) -> None:
            # Extract highlights if available on profile response
            for h in self._extract_highlights_from_json(data):
                if h not in captured_highlights:
                    captured_highlights.append(h)

            # Extract media items based on current active tab
            tab = current["tab"]
            if tab is None:
                return
            extracted = self._extract_media_from_json(data, tab=tab)
            found[tab] += len(extracted)
            for item in extracted:
                self._queue_media(item, target_dir / tab, client, files, headers, sniff_video=True)

        with browser.open(headless=headless, user_agent=INSTAGRAM_DESKTOP_UA, cookies=cookies_list) as page:
            browser.capture(page, is_profile_api, on_profile)

            # Navigate through each tab
            for tab_name, tab_path in TABS:
                if files.full:
                    break

                current["tab"] = tab_name
                tab_url = f"https://www.instagram.com/{username}/{tab_path}"
                log.info("Navigating tab {tab}: {url}", tab=tab_name, url=tab_url)

                try:
                    page.goto(tab_url, wait_until="load", timeout=25000)
                    page.wait_for_timeout(3000)

                    # Continuously scroll down to the end to trigger all GraphQL replies
                    scroll(page, tab_name, count=lambda: found[tab_name], more=lambda: not files.full)

                except Exception as e:
                    log.warn("Navigation to {url} encountered: {err}", url=tab_url, err=e)

                log.info("{tab}: intercepted {n} media items.", tab=tab_name, n=found[tab_name])

            # Media seen after the tabs was never downloaded; only highlights are still collected.
            current["tab"] = None

            # 3. Download highlights if found on profile
            if captured_highlights and not files.full:
                log.info("Found {n} highlight collections.", n=len(captured_highlights))

                # Return to the main profile page so highlight elements can be clicked
                profile_url = f"https://www.instagram.com/{username}/"
                if not page.url.rstrip("/").endswith(f"instagram.com/{username}"):
                    try:
                        page.goto(profile_url, wait_until="load", timeout=20000)
                        page.wait_for_timeout(2500)
                    except Exception:
                        pass

                for hl in captured_highlights:
                    if files.full:
                        break
                    self._download_highlight(page, hl, target_dir, client, files)

    def _download_highlight(
        self, page: Any, hl: Dict[str, Any], target_dir: Path, client: http.Client, files: Downloads
    ) -> None:
        """Open one highlight and queue the stories it loads."""
        hl_id = hl["id"].replace("highlight:", "")
        hl_title = hl["title"]
        hl_dir = target_dir / "highlights" / path.safename(hl_title)
        hl_url = f"https://www.instagram.com/stories/highlights/{hl_id}/"

        log.info("Opening highlight '{title}': {url}", title=hl_title, url=hl_url)
        found = [0]

        def on_highlight(response: Any, data: Any) -> None:
            parsed = self._extract_stories_from_json(data)
            found[0] += len(parsed)
            for s_item in parsed:
                self._queue_media(s_item, hl_dir, client, files, None, sniff_video=False)

        stop = browser.capture(page, is_profile_api, on_highlight)

        # Click highlight element on profile page to trigger client-side Relay query
        try:
            hl_locator = page.locator(f'a[href*="{hl_id}"]').first
            if hl_locator.count() == 0:
                hl_locator = page.get_by_text(hl["title"].replace("_", " "), exact=False).first

            if hl_locator and hl_locator.count() > 0:
                hl_locator.click()
                page.wait_for_timeout(3500)
                # Close story modal so subsequent highlights can be clicked
                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
            else:
                # Fallback to direct navigation
                page.goto(hl_url, wait_until="load", timeout=20000)
                page.wait_for_timeout(3500)
        except Exception as e:
            log.warn("Could not open highlight '{title}': {err}", title=hl_title, err=e)
        stop()

        log.info("Highlight '{title}': intercepted {n} items.", title=hl_title, n=found[0])

    def _queue_media(
        self,
        item: Dict[str, Any],
        dest_dir: Path,
        client: http.Client,
        files: Downloads,
        headers: Optional[Dict[str, str]],
        sniff_video: bool,
    ) -> None:
        """Queue each file of a post or story. Posts also count a `.mp4` URL as a video."""
        date_str = item.get("date") or ""
        post_id = item["id"]

        for idx, media_url in enumerate(item.get("media_urls", [])):
            is_video = item.get("is_video", False) or (sniff_video and ".mp4" in media_url)
            ext = "mp4" if is_video else "jpg"
            stem = f"{post_id}_{idx}" if len(item["media_urls"]) > 1 else post_id
            filename = f"{date_str}_{stem}.{ext}" if date_str else f"{stem}.{ext}"
            files.add(client, media_url, dest_dir / filename, key=stem, headers=headers)

    def _extract_media_from_json(self, data: Dict[str, Any], tab: str = "posts") -> List[Dict[str, Any]]:
        """
        Extract media items from Instagram GraphQL JSON for posts, reels, tagged, or reposts.
        """
        extracted = []
        try:
            edges = []

            # 1. Main timeline posts
            if tab == "posts":
                timeline = scrape.dig(
                    data,
                    "data.xdt_api__v1__feed__user_timeline_graphql_connection"
                    " || data.user.edge_owner_to_timeline_media",
                    default={},
                )
                edges = timeline.get("edges", [])

            # 2. Reels tab
            elif tab == "reels":
                clips = scrape.dig(
                    data,
                    "data.fetch__XDTUserDict.clips_connection"
                    " || data.xdt_api__v1__clips__user__connection_v2"
                    " || data.user.edge_felix_video_timeline",
                    default={},
                )
                edges = clips.get("edges", [])

            # 3. Tagged tab
            elif tab == "tagged":
                tagged_conn = scrape.dig(
                    data,
                    "data.xdt_api__v1__usertags__user_id__feed_connection || data.user.edge_user_to_photos_of_you",
                    default={},
                )
                if tagged_conn:
                    edges = tagged_conn.get("edges", [])
                else:
                    # Generic search for usertag feeds in JSON
                    for k, v in data.get("data", {}).items():
                        if "usertag" in k.lower() and isinstance(v, dict):
                            edges = v.get("edges", [])
                            break

            # 4. Reposts tab
            elif tab == "reposts":
                reposts_conn = scrape.dig(
                    data,
                    "data.fetch__XDTUserDict.user_reposts_timeline"
                    " || data.xdt_api__v1__feed__user_reposts_graphql_connection"
                    " || data.user_reposts_timeline",
                    default={},
                )
                if "repost_grid_items" in reposts_conn:
                    edges = [{"node": item.get("media", item)} for item in reposts_conn.get("repost_grid_items", [])]
                elif "edges" in reposts_conn:
                    edges = reposts_conn.get("edges", [])
                else:
                    for k, v in data.get("data", {}).items():
                        if "repost" in k.lower() and isinstance(v, dict):
                            if "repost_grid_items" in v:
                                edges = [{"node": item.get("media", item)} for item in v["repost_grid_items"]]
                            elif "edges" in v:
                                edges = v["edges"]
                            break

            # Fallback if edges empty: check generic items list
            if not edges and "items" in data:
                edges = [{"node": item} for item in data.get("items", [])]

            for edge in edges:
                outer_node = edge.get("node", {})
                node = outer_node
                # If node has an inner 'media' dictionary (common in reels / clips_connection)
                if "media" in outer_node and isinstance(outer_node["media"], dict):
                    node = outer_node["media"]

                post_id = str(node.get("code") or node.get("shortcode") or outer_node.get("code") or node.get("pk") or node.get("id") or "")
                if not post_id:
                    continue

                ts = (
                    node.get("taken_at")
                    or node.get("taken_at_timestamp")
                    or outer_node.get("taken_at")
                    or outer_node.get("taken_at_timestamp")
                )
                date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""

                media_urls = []
                is_video = False

                # Carousel / multi-photo slides
                carousel = node.get("carousel_media", [])
                if carousel:
                    for child in carousel:
                        videos = child.get("video_versions", [])
                        images = scrape.dig(child, "image_versions2.candidates", default=[])
                        if videos:
                            media_urls.append(videos[0]["url"])
                            is_video = True
                        elif images:
                            media_urls.append(images[0]["url"])

                # Legacy edge_sidecar_to_children
                elif scrape.dig(node, "edge_sidecar_to_children.edges"):
                    for child in node["edge_sidecar_to_children"]["edges"]:
                        c_node = child.get("node", {})
                        if c_node.get("is_video") and c_node.get("video_url"):
                            media_urls.append(c_node["video_url"])
                            is_video = True
                        elif c_node.get("display_url"):
                            media_urls.append(c_node["display_url"])

                # Single photo or video item
                else:
                    videos = node.get("video_versions", [])
                    images = scrape.dig(node, "image_versions2.candidates", default=[])
                    if videos:
                        media_urls.append(videos[0]["url"])
                        is_video = True
                    elif node.get("video_url"):
                        media_urls.append(node["video_url"])
                        is_video = True
                    elif images:
                        media_urls.append(images[0]["url"])
                    elif node.get("display_url"):
                        media_urls.append(node["display_url"])

                if media_urls:
                    # Deduplicate while preserving order
                    seen_urls = set()
                    deduped_urls = [u for u in media_urls if not (u in seen_urls or seen_urls.add(u))]
                    extracted.append({
                        "id": post_id,
                        "date": date_str,
                        "is_video": is_video or (tab == "reels"),
                        "media_urls": deduped_urls,
                    })

        except Exception as e:
            log.warn("Error parsing Instagram JSON for {tab}: {err}", tab=tab, err=e)

        return extracted

    def _extract_highlights_from_json(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract highlight collections from profile GraphQL."""
        highlights = []
        try:
            edges = scrape.dig(data, "data.highlights.edges", default=[])
            for edge in edges:
                node = edge.get("node", {})
                hid = node.get("id")
                title = path.safename(node.get("title", "highlight"))
                if hid:
                    highlights.append({"id": hid, "title": title})
        except Exception:
            pass
        return highlights

    def _extract_stories_from_json(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract story/highlight media items from GraphQL connection."""
        items = []
        try:
            nodes = scrape.dig(
                data, "data.xdt_api__v1__feed__reels_media__connection.edges || data.reels_media", default=[]
            )
            for node in nodes:
                reel = node.get("node", {}) if "node" in node else node
                for item in reel.get("items", []):
                    story_id = str(item.get("id") or item.get("pk") or "").split("_")[0]
                    ts = item.get("taken_at")
                    date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""

                    media_urls = []
                    is_video = False
                    if item.get("media_type") == 2 and item.get("video_versions"):
                        media_urls.append(item["video_versions"][0]["url"])
                        is_video = True
                    elif scrape.dig(item, "image_versions2.candidates"):
                        media_urls.append(item["image_versions2"]["candidates"][0]["url"])

                    if media_urls:
                        items.append({
                            "id": story_id,
                            "date": date_str,
                            "is_video": is_video,
                            "media_urls": media_urls,
                        })
        except Exception:
            pass
        return items
