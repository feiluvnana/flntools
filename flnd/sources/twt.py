"""
Twitter / X source extractor for flnd.

Features:
  - Reads cookies (auth_token, ct0) from .flnd/cookies/twt.txt
  - Downloads media from user Posts timeline into posts/
  - Downloads full-resolution photos (?name=orig) into images/
  - Downloads highest-bitrate MP4 videos into videos/
  - Supports user posts and media timelines, and single tweet IDs
  - Continuously scrolls each tab to the end with cursor pagination

Optimized folder layout:
  .flnd/downloads/twt/<username>/
  ├── posts/<YYYY-MM-DD>_<tweet_id>_<idx>.jpg (or .mp4)
  ├── images/<YYYY-MM-DD>_<tweet_id>_<idx>.jpg
  └── videos/<YYYY-MM-DD>_<tweet_id>_<idx>.mp4
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx

from flnd.cookies import cookies_to_dict, load_cookies, prompt_missing_cookies
from flnd.sources.base import BaseSource
from flnd.target import Target
from flnd.utils import download_file, scroll_page_to_end

# Twitter's standard web client public bearer token
TWITTER_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# GraphQL query IDs used by Twitter Web
QUERY_USER_BY_SCREEN_NAME_PRIMARY = "AMIBMjtxEEATh4z8V9GtRg/UserByScreenName"
QUERY_USER_BY_SCREEN_NAME_LEGACY = "ck5KkZ8t5cOmoLssopN99Q/UserByScreenName"
QUERY_USER_ORIGINALS_TIMELINE = "ZFKu1DFS63hgrkLzR3Hsdw/UserOriginalsTimeline"
QUERY_USER_TWEETS = "E3opETHurGV11kgpTmUZ_Q/UserTweets"
QUERY_USER_MEDIA = "jCRhbOzdgOHp6u9H4g2tEg/UserMedia"
QUERY_TWEET_DETAIL = "2ICDjqPd81tulZcYrtpTuQ/TweetResultByRestId"


class TwtSource(BaseSource):
    """Twitter / X extractor with autonomous scraping and media downloading."""

    code = "twt"

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

        if "auth_token" not in cookie_map:
            prompt_missing_cookies(self.code)
            print("  [warn] Running without auth_token; only public guest access will be available.")

        target_dir = download_dir / "twt" / username_or_id
        posts_dir = target_dir / "posts"
        images_dir = target_dir / "images"
        videos_dir = target_dir / "videos"
        posts_dir.mkdir(parents=True, exist_ok=True)
        images_dir.mkdir(parents=True, exist_ok=True)
        videos_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n[twt] Starting download for: {username_or_id}")
        print(f"      Destination: {target_dir}")

        # Check if target is a numeric ID (single tweet)
        if username_or_id.isdigit() and len(username_or_id) > 10:
            return self._download_single_tweet(username_or_id, target_dir, cookie_map)

        # Otherwise, treat as username and download user posts & media timeline
        return self._download_user_timeline(username_or_id, target_dir, cookie_map, limit, headless)

    def _get_headers(self, cookie_map: Dict[str, str]) -> Dict[str, str]:
        """Construct Twitter API headers with bearer token and CSRF."""
        csrf = cookie_map.get("ct0", "")
        cookie_str = "; ".join([f"{k}={v}" for k, v in cookie_map.items()])

        return {
            "authorization": TWITTER_BEARER,
            "x-csrf-token": csrf,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session" if "auth_token" in cookie_map else "",
            "x-twitter-client-language": "en",
            "cookie": cookie_str,
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
        }

    def _download_user_media(
        self,
        screen_name: str,
        target_dir: Path,
        cookie_map: Dict[str, str],
        limit: Optional[int],
        headless: bool,
    ) -> int:
        """Alias for backward compatibility."""
        return self._download_user_timeline(screen_name, target_dir, cookie_map, limit, headless)

    def _download_user_timeline(
        self,
        screen_name: str,
        target_dir: Path,
        cookie_map: Dict[str, str],
        limit: Optional[int],
        headless: bool,
    ) -> int:
        """Fetch user posts and media timeline using Twitter GraphQL API with Playwright fallback."""
        headers = self._get_headers(cookie_map)

        with httpx.Client(headers=headers, timeout=20.0) as client:
            # 1. Resolve user ID from screen name
            user_id = self._resolve_user_id(client, screen_name)
            if not user_id:
                print(f"  [warn] Direct API resolution failed for '{screen_name}', attempting Playwright browser...")
                return self._download_via_playwright(screen_name, target_dir, cookie_map, limit, headless)

            print(f"  [info] Resolved @{screen_name} -> User ID: {user_id}")

            downloaded_count = 0

            # 2. Iterate through user timelines: Posts then Media
            timelines = [
                ("posts", QUERY_USER_ORIGINALS_TIMELINE),
                ("media", QUERY_USER_MEDIA),
            ]

            for category, query_id in timelines:
                if limit and downloaded_count >= limit:
                    break

                print(f"  [info] Fetching {category} timeline for @{screen_name}...")
                cursor = None
                empty_page_count = 0

                while True:
                    variables: Dict[str, Any] = {
                        "userId": user_id,
                        "count": 20,
                        "includePromotedContent": False,
                        "withClientEventToken": False,
                        "withBirdwatchNotes": False,
                        "withVoice": True,
                    }
                    if category == "posts":
                        variables["withQuickPromoteEligibilityTweetFields"] = True

                    if cursor:
                        variables["cursor"] = cursor

                    features = {
                        "responsive_web_graphql_timeline_navigation_enabled": True,
                        "responsive_web_media_download_video_enabled": True,
                    }

                    url = f"https://x.com/i/api/graphql/{query_id}"
                    params = {"variables": json.dumps(variables), "features": json.dumps(features)}

                    try:
                        resp = client.get(url, params=params)
                        if resp.status_code != 200:
                            print(f"  [warn] API status {resp.status_code} on {category}, switching to browser extraction...")
                            return downloaded_count + self._download_via_playwright(
                                screen_name, target_dir, cookie_map, limit, headless, current_count=downloaded_count
                            )

                        data = resp.json()
                    except Exception as e:
                        print(f"  [error] API request error: {e}")
                        break

                    # Extract media items from timeline instructions
                    items, next_cursor = self._parse_timeline_entries(data)
                    if not items:
                        empty_page_count += 1
                        if empty_page_count >= 5:
                            break
                    else:
                        empty_page_count = 0

                    for item in items:
                        item["category"] = category
                        saved = self._save_media_item(item, target_dir, client)
                        if saved:
                            downloaded_count += 1
                            if limit and downloaded_count >= limit:
                                print(f"\n[twt] Reached target limit of {limit} downloads.")
                                return downloaded_count

                    if not next_cursor or next_cursor == cursor or next_cursor.startswith("0|"):
                        break
                    cursor = next_cursor

            print(f"\n[twt] Finished. Downloaded {downloaded_count} items for @{screen_name}.")
            return downloaded_count

    def _resolve_user_id(self, client: httpx.Client, screen_name: str) -> Optional[str]:
        """Query UserByScreenName GraphQL endpoint to obtain numeric rest_id."""
        for query in [QUERY_USER_BY_SCREEN_NAME_PRIMARY, QUERY_USER_BY_SCREEN_NAME_LEGACY]:
            url = f"https://x.com/i/api/graphql/{query}"
            params = {
                "variables": json.dumps({"screen_name": screen_name, "withSafetyModeUserFields": True}),
                "features": json.dumps({"hidden_profile_likes_enabled": True}),
            }
            try:
                r = client.get(url, params=params)
                if r.status_code == 200:
                    user_res = r.json().get("data", {}).get("user", {}).get("result", {})
                    rest_id = user_res.get("rest_id")
                    if rest_id:
                        return rest_id
            except Exception:
                pass
        return None

    def _parse_timeline_entries(self, data: Dict[str, Any]) -> tuple[List[Dict[str, Any]], Optional[str]]:
        """Parse tweets and pagination cursor from GraphQL timeline JSON response."""
        media_items = []
        next_cursor = None

        try:
            user_result = data.get("data", {}).get("user", {}).get("result", {})
            timeline_obj = (
                user_result.get("timeline_v2", {}).get("timeline", {})
                or user_result.get("timeline", {}).get("timeline", {})
                or user_result.get("timeline", {})
            )
            instructions = timeline_obj.get("instructions", [])

            for instruction in instructions:
                entries = instruction.get("entries", [])
                if "entry" in instruction:
                    entries.append(instruction["entry"])

                for entry in entries:
                    content = entry.get("content", {})
                    entry_id = entry.get("entryId", "")

                    # Check for pagination cursor
                    if content.get("cursorType") == "Bottom" or "cursor-bottom" in entry_id:
                        next_cursor = content.get("value")

                    # Extract tweet results (both flat itemContent and grid items module)
                    raw_items = []
                    if "items" in content:
                        for grid_item in content["items"]:
                            tc = grid_item.get("item", {}).get("itemContent", {}) or grid_item.get("itemContent", {})
                            raw_items.append(tc)
                    elif "itemContent" in content:
                        raw_items.append(content["itemContent"])

                    for item_content in raw_items:
                        tweet_result = item_content.get("tweet_results", {}).get("result", {})
                        if not tweet_result:
                            continue

                        # Unwrap tweet if nested under tweet
                        if "tweet" in tweet_result:
                            tweet_result = tweet_result["tweet"]

                        legacy = tweet_result.get("legacy", {})
                        tweet_id = tweet_result.get("rest_id") or legacy.get("id_str")
                        created_at = legacy.get("created_at")

                        # Parse date string (e.g. 'Tue Nov 10 14:22:30 +0000 2026')
                        date_str = ""
                        if created_at:
                            try:
                                dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
                                date_str = dt.strftime("%Y-%m-%d")
                            except Exception:
                                date_str = ""

                        # Extract extended entities media (photos & videos)
                        entities = legacy.get("extended_entities", {}).get("media", [])
                        for idx, media in enumerate(entities):
                            media_type = media.get("type")
                            media_id = str(media.get("id_str") or idx)

                            if media_type == "photo":
                                base_url = media.get("media_url_https", "")
                                if base_url:
                                    media_items.append({
                                        "kind": "image",
                                        "url": f"{base_url}?name=orig",
                                        "tweet_id": tweet_id,
                                        "media_id": media_id,
                                        "date": date_str,
                                        "idx": idx,
                                    })

                            elif media_type in ("video", "animated_gif"):
                                variants = media.get("video_info", {}).get("variants", [])
                                mp4s = [v for v in variants if v.get("content_type") == "video/mp4"]
                                if mp4s:
                                    best = max(mp4s, key=lambda v: v.get("bitrate", 0))
                                    media_items.append({
                                        "kind": "video",
                                        "url": best.get("url"),
                                        "tweet_id": tweet_id,
                                        "media_id": media_id,
                                        "date": date_str,
                                        "idx": idx,
                                    })

        except Exception as e:
            print(f"  [warn] Error parsing timeline JSON: {e}")

        return media_items, next_cursor

    def _save_media_item(self, item: Dict[str, Any], target_dir: Path, client: httpx.Client) -> bool:
        """Save an extracted Twitter media item into the platform-optimized layout."""
        category = item.get("category", "media")
        kind = item["kind"]
        tweet_id = item["tweet_id"]
        date_str = item.get("date") or ""
        idx = item.get("idx", 0)
        ext = "jpg" if kind == "image" else "mp4"

        filename = f"{date_str}_{tweet_id}_{idx}.{ext}" if date_str else f"{tweet_id}_{idx}.{ext}"
        if category == "posts":
            dest_folder = target_dir / "posts"
        else:
            dest_folder = target_dir / ("images" if kind == "image" else "videos")
        dest_path = dest_folder / filename

        # Check deduplication across all subfolders for this user to avoid re-downloading duplicate files
        for check_folder in [target_dir / "posts", target_dir / "images", target_dir / "videos"]:
            existing = check_folder / filename
            if existing.is_file() and existing.stat().st_size > 0:
                print(f"  [skip] Already downloaded: {dest_folder.name}/{filename}")
                return False

        return download_file(item["url"], dest_path, client=client)

    def _download_single_tweet(
        self,
        tweet_id: str,
        target_dir: Path,
        cookie_map: Dict[str, str],
    ) -> int:
        """Download media for a single status / tweet ID."""
        headers = self._get_headers(cookie_map)
        url = f"https://x.com/i/api/graphql/{QUERY_TWEET_DETAIL}"
        params = {
            "variables": json.dumps({"tweetId": tweet_id, "withCommunity": False, "includePromotedContent": False}),
            "features": json.dumps({"responsive_web_graphql_timeline_navigation_enabled": True}),
        }

        with httpx.Client(headers=headers, timeout=20.0) as client:
            resp = client.get(url, params=params)
            if resp.status_code != 200:
                print(f"  [fail] HTTP {resp.status_code} fetching tweet {tweet_id}")
                return 0

            # Wrap in structure compatible with timeline parser
            data = resp.json()
            tweet_result = data.get("data", {}).get("tweetResult", {}).get("result", {})
            fake_timeline = {"data": {"user": {"result": {"timeline_v2": {"timeline": {"instructions": [
                {"entries": [{"content": {"itemContent": {"tweet_results": {"result": tweet_result}}}}]}
            ]}}}}}}

            items, _ = self._parse_timeline_entries(fake_timeline)
            saved_count = 0
            for item in items:
                item["category"] = "posts"
                if self._save_media_item(item, target_dir, client):
                    saved_count += 1

            return saved_count

    def _download_via_playwright(
        self,
        screen_name: str,
        target_dir: Path,
        cookie_map: Dict[str, str],
        limit: Optional[int],
        headless: bool,
        current_count: int = 0,
    ) -> int:
        """Playwright fallback: navigates to x.com/<user> and x.com/<user>/media, scrolling to the end and intercepting GraphQL replies."""
        from playwright.sync_api import sync_playwright

        cookies_list = load_cookies(self.code)
        print(f"  [browser] Launching Playwright Chromium for @{screen_name}...")
        captured_items: List[Dict[str, Any]] = []

        tabs = [
            ("posts", f"https://x.com/{screen_name}"),
            ("media", f"https://x.com/{screen_name}/media"),
        ]

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled", "--disable-infobars"],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
            )

            # Add cookies to context
            if cookies_list:
                formatted_cookies = []
                for c in cookies_list:
                    fc = {
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c.get("domain", ".x.com"),
                        "path": c.get("path", "/"),
                    }
                    if not fc["domain"].startswith("."):
                        fc["domain"] = "." + fc["domain"]
                    formatted_cookies.append(fc)
                try:
                    context.add_cookies(formatted_cookies)
                except Exception as e:
                    print(f"  [warn] Could not set cookies in browser: {e}")

            page = context.new_page()
            current_category = ["posts"]

            # Intercept GraphQL responses
            def on_response(response):
                url = response.url
                if "graphql" in url:
                    if any(endpoint in url for endpoint in ["UserMedia", "UserOriginalsTimeline", "UserTweets", "UserTweetsAndReplies"]):
                        try:
                            data = response.json()
                            items, _ = self._parse_timeline_entries(data)
                            for it in items:
                                it["category"] = current_category[0]
                            captured_items.extend(items)
                        except Exception:
                            pass

            page.on("response", on_response)

            for cat, tab_url in tabs:
                if limit and (current_count + len(captured_items)) >= limit:
                    break
                current_category[0] = cat
                print(f"  [browser] Navigating [{cat}]: {tab_url}")
                try:
                    page.goto(tab_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3000)

                    # Continuously scroll to the end of the tab
                    scroll_page_to_end(
                        page,
                        get_count=lambda: len(captured_items),
                        limit=limit,
                        max_idle_rounds=4,
                        step_delay_ms=2000,
                    )
                except Exception as e:
                    print(f"  [warn] Browser navigation error on {tab_url}: {e}")

            browser.close()

        print(f"  [browser] Captured {len(captured_items)} items from browser session.")
        saved_count = 0
        with httpx.Client(timeout=30.0) as client:
            for item in captured_items:
                if self._save_media_item(item, target_dir, client):
                    saved_count += 1
                    if limit and (current_count + saved_count) >= limit:
                        break

        return saved_count
