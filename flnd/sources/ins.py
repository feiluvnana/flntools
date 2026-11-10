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
from playwright.sync_api import sync_playwright

from flnd.cookies import cookies_to_dict, load_cookies, prompt_missing_cookies
from flnd.sources.base import BaseSource
from flnd.target import Target
from flnd.utils import download_file, sanitize_name, scroll_page_to_end

# Mobile Android User-Agent: Instagram story tray API refuses desktop user-agents
INSTAGRAM_MOBILE_UA = (
    "Instagram 219.0.0.12.117 Android (33/13; 420dpi; 1080x2400; samsung; SM-G991B; o1s; exynos2100; en_US; 346132297)"
)

INSTAGRAM_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class InsSource(BaseSource):
    """Instagram autonomous scraper supporting posts, reels, tagged, reposts, stories, and highlights."""

    code = "ins"

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

        if "sessionid" not in cookie_map:
            prompt_missing_cookies(self.code)
            print("  [warn] Running without sessionid; Instagram will likely wall requests.")

        target_dir = download_dir / "ins" / username
        target_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n[ins] Starting download for: @{username}")
        print(f"      Destination: {target_dir}")
        print(f"      Browser mode: {'headless' if headless else 'non-headless (visible)'}")

        total_downloaded = 0

        with httpx.Client(timeout=30.0) as http_client:
            # 1. Fetch active 24h stories via mobile API tray
            user_id = cookie_map.get("ds_user_id")
            if user_id:
                print("  [stories] Checking active 24h stories tray...")
                story_count = self._download_active_stories(username, user_id, target_dir, cookie_map, http_client)
                total_downloaded += story_count
                if limit and total_downloaded >= limit:
                    print(f"\n[ins] Reached limit of {limit} downloads.")
                    return total_downloaded

            # 2. Launch browser to navigate profile tabs: posts, reels, tagged, reposts, highlights
            browser_count = self._download_via_browser(
                username, target_dir, cookies_list, limit, headless, http_client, current_count=total_downloaded
            )
            total_downloaded += browser_count

        print(f"\n[ins] Finished. Total downloaded for @{username}: {total_downloaded}")
        return total_downloaded

    def _download_active_stories(
        self,
        username: str,
        user_id: str,
        target_dir: Path,
        cookie_map: Dict[str, str],
        client: httpx.Client,
    ) -> int:
        """Download active user stories via Instagram's mobile reels_media API."""
        cookie_str = "; ".join([f"{k}={v}" for k, v in cookie_map.items()])
        headers = {
            "user-agent": INSTAGRAM_MOBILE_UA,
            "cookie": cookie_str,
            "x-ig-app-id": "936619743392459",
        }

        url = "https://i.instagram.com/api/v1/feed/reels_media/"
        params = {"user_ids": json.dumps([user_id]), "source": "profile"}

        try:
            resp = client.get(url, headers=headers, params=params)
            if resp.status_code != 200:
                return 0

            data = resp.json()
            reels = data.get("reels_media", []) or list(data.get("reels", {}).values())
            stories_dir = target_dir / "stories"
            saved = 0

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
                        if download_file(best_video, dest, client=client):
                            saved += 1
                    elif item.get("image_versions2", {}).get("candidates"):
                        best_image = item["image_versions2"]["candidates"][0]["url"]
                        dest = stories_dir / f"{date_str}_{story_id}.jpg"
                        if download_file(best_image, dest, client=client):
                            saved += 1

            if saved:
                print(f"  [stories] Downloaded {saved} active stories from mobile tray.")
            return saved

        except Exception as e:
            print(f"  [warn] Could not fetch stories from mobile API: {e}")
            return 0

    def _download_via_browser(
        self,
        username: str,
        target_dir: Path,
        cookies_list: List[Dict[str, Any]],
        limit: Optional[int],
        headless: bool,
        client: httpx.Client,
        current_count: int = 0,
    ) -> int:
        """
        Use Playwright browser to navigate all user profile tabs:
          - / (Posts)
          - /reels/ (Reels)
          - /tagged/ (Tagged posts)
          - /reposts/ (Reposts, if enabled)
          - Highlights collections
        Intercepts GraphQL network responses and downloads media into platform-optimized subfolders.
        """
        saved_count = 0
        captured_highlights: List[Dict[str, Any]] = []

        # Tabs to navigate in order: (folder_name, relative_path)
        tabs = [
            ("posts", ""),
            ("reels", "reels/"),
            ("tagged", "tagged/"),
            ("reposts", "reposts/"),
        ]

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled", "--disable-infobars"],
            )
            context = browser.new_context(
                user_agent=INSTAGRAM_DESKTOP_UA,
                viewport={"width": 1280, "height": 900},
            )

            # Inject session cookies into browser context
            if cookies_list:
                formatted_cookies = []
                for c in cookies_list:
                    fc = {
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c.get("domain", ".instagram.com"),
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

            # Store media items captured per tab
            tab_media: Dict[str, List[Dict[str, Any]]] = {
                "posts": [],
                "reels": [],
                "tagged": [],
                "reposts": [],
            }
            current_tab = "posts"

            # Listen to background GraphQL and API responses
            def on_response(response):
                url = response.url
                if "graphql" in url or "/api/v1/" in url:
                    try:
                        data = response.json()
                        # Extract highlights if available on profile response
                        hl = self._extract_highlights_from_json(data)
                        if hl:
                            for h in hl:
                                if h not in captured_highlights:
                                    captured_highlights.append(h)

                        # Extract media items based on current active tab
                        extracted = self._extract_media_from_json(data, tab=current_tab)
                        if extracted:
                            tab_media[current_tab].extend(extracted)
                    except Exception:
                        pass

            page.on("response", on_response)

            # Navigate through each tab
            for tab_name, tab_path in tabs:
                if limit and (current_count + saved_count) >= limit:
                    break

                current_tab = tab_name
                tab_url = f"https://www.instagram.com/{username}/{tab_path}"
                print(f"  [browser] Navigating tab: [{tab_name}] {tab_url}")

                try:
                    page.goto(tab_url, wait_until="load", timeout=25000)
                    page.wait_for_timeout(3000)

                    # Continuously scroll down to the end to trigger all GraphQL replies
                    scroll_page_to_end(
                        page,
                        get_count=lambda: current_count + saved_count + len(tab_media[tab_name]),
                        limit=limit,
                        max_idle_rounds=4,
                        step_delay_ms=2000,
                    )

                except Exception as e:
                    print(f"  [warn] Navigation to {tab_url} encountered: {e}")

                # Download items gathered for this tab immediately
                dest_dir = target_dir / tab_name
                items = tab_media[tab_name]
                print(f"  [tab:{tab_name}] Intercepted {len(items)} media items.")

                for item in items:
                    date_str = item.get("date") or ""
                    post_id = item["id"]

                    for idx, media_url in enumerate(item.get("media_urls", [])):
                        is_video = item.get("is_video", False) or (".mp4" in media_url)
                        ext = "mp4" if is_video else "jpg"
                        if len(item["media_urls"]) > 1:
                            filename = f"{date_str}_{post_id}_{idx}.{ext}" if date_str else f"{post_id}_{idx}.{ext}"
                        else:
                            filename = f"{date_str}_{post_id}.{ext}" if date_str else f"{post_id}.{ext}"
                        dest_path = dest_dir / filename

                        headers = {"Referer": f"https://www.instagram.com/{username}/"}
                        if download_file(media_url, dest_path, headers=headers, client=client):
                            saved_count += 1
                            if limit and (current_count + saved_count) >= limit:
                                print(f"\n[ins] Reached limit of {limit} downloads.")
                                browser.close()
                                return saved_count

            # 3. Download highlights if found on profile
            if captured_highlights and (not limit or (current_count + saved_count) < limit):
                print(f"  [highlights] Found {len(captured_highlights)} highlight collections.")
                for hl in captured_highlights:
                    hl_id = hl["id"].replace("highlight:", "")
                    hl_title = hl["title"]
                    hl_dir = target_dir / "highlights" / sanitize_name(hl_title)
                    hl_url = f"https://www.instagram.com/stories/highlights/{hl_id}/"

                    print(f"  [highlight] Opening '{hl_title}': {hl_url}")
                    hl_items: List[Dict[str, Any]] = []

                    def on_hl_response(resp):
                        if "graphql" in resp.url or "/api/v1/" in resp.url:
                            try:
                                d = resp.json()
                                parsed = self._extract_stories_from_json(d)
                                hl_items.extend(parsed)
                            except Exception:
                                pass

                    page.on("response", on_hl_response)
                    try:
                        page.goto(hl_url, wait_until="load", timeout=20000)
                        page.wait_for_timeout(3000)
                    except Exception:
                        pass

                    for s_item in hl_items:
                        date_str = s_item.get("date") or datetime.now().strftime("%Y-%m-%d")
                        s_id = s_item["id"]
                        for idx, m_url in enumerate(s_item.get("media_urls", [])):
                            ext = "mp4" if s_item.get("is_video") else "jpg"
                            filename = f"{date_str}_{s_id}_{idx}.{ext}" if len(s_item["media_urls"]) > 1 else f"{date_str}_{s_id}.{ext}"
                            dest_path = hl_dir / filename
                            if download_file(m_url, dest_path, client=client):
                                saved_count += 1
                                if limit and (current_count + saved_count) >= limit:
                                    browser.close()
                                    return saved_count

            browser.close()

        return saved_count

    def _extract_media_from_json(self, data: Dict[str, Any], tab: str = "posts") -> List[Dict[str, Any]]:
        """
        Extract media items from Instagram GraphQL JSON for posts, reels, tagged, or reposts.
        """
        extracted = []
        try:
            edges = []

            # 1. Main timeline posts
            if tab == "posts":
                timeline = (
                    data.get("data", {}).get("xdt_api__v1__feed__user_timeline_graphql_connection", {})
                    or data.get("data", {}).get("user", {}).get("edge_owner_to_timeline_media", {})
                )
                edges = timeline.get("edges", [])

            # 2. Reels tab
            elif tab == "reels":
                clips = (
                    data.get("data", {}).get("fetch__XDTUserDict", {}).get("clips_connection", {})
                    or data.get("data", {}).get("xdt_api__v1__clips__user__connection_v2", {})
                    or data.get("data", {}).get("user", {}).get("edge_felix_video_timeline", {})
                )
                edges = clips.get("edges", [])

            # 3. Tagged tab
            elif tab == "tagged":
                tagged_conn = (
                    data.get("data", {}).get("xdt_api__v1__usertags__user_id__feed_connection", {})
                    or data.get("data", {}).get("user", {}).get("edge_user_to_photos_of_you", {})
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
                xdt_dict = data.get("data", {}).get("fetch__XDTUserDict", {})
                reposts_conn = (
                    xdt_dict.get("user_reposts_timeline", {})
                    or data.get("data", {}).get("xdt_api__v1__feed__user_reposts_graphql_connection", {})
                    or data.get("data", {}).get("user_reposts_timeline", {})
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
                        images = child.get("image_versions2", {}).get("candidates", [])
                        if videos:
                            media_urls.append(videos[0]["url"])
                            is_video = True
                        elif images:
                            media_urls.append(images[0]["url"])

                # Legacy edge_sidecar_to_children
                elif node.get("edge_sidecar_to_children", {}).get("edges"):
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
                    images = node.get("image_versions2", {}).get("candidates", [])
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
            print(f"  [warn] Error parsing Instagram JSON for [{tab}]: {e}")

        return extracted

    def _extract_highlights_from_json(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract highlight collections from profile GraphQL."""
        highlights = []
        try:
            edges = data.get("data", {}).get("highlights", {}).get("edges", [])
            for edge in edges:
                node = edge.get("node", {})
                hid = node.get("id")
                title = sanitize_name(node.get("title", "highlight"))
                if hid:
                    highlights.append({"id": hid, "title": title})
        except Exception:
            pass
        return highlights

    def _extract_stories_from_json(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract story/highlight media items from GraphQL connection."""
        items = []
        try:
            nodes = (
                data.get("data", {}).get("xdt_api__v1__feed__reels_media__connection", {}).get("edges", [])
                or data.get("data", {}).get("reels_media", [])
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
                    elif item.get("image_versions2", {}).get("candidates"):
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
