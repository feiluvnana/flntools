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

from flnd.cookies import ask_for_cookies, cookies_to_dict, load_cookies
from flnd.sources.base import BaseSource
from flnd.target import Target
from flnd.utils import Downloads, scroll
from shared import browser, http, log, path, scrape

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

TIMELINE_ENDPOINTS = ["UserMedia", "UserOriginalsTimeline", "UserTweets", "UserTweetsAndReplies"]


def is_timeline(response: Any) -> bool:
    return "graphql" in response.url and any(endpoint in response.url for endpoint in TIMELINE_ENDPOINTS)


class TwtSource(BaseSource):
    """Twitter / X extractor with autonomous scraping and media downloading."""

    code = "twt"
    name = "Twitter/X"
    domains = ("x.com", "twitter.com")
    # This target's downloads, set while `download` runs.
    files: Optional[Downloads] = None

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

        if "auth_token" not in cookie_map and ask_for_cookies(self.code):
            cookies_list = load_cookies(self.code)
            cookie_map = cookies_to_dict(cookies_list)
        if "auth_token" not in cookie_map:
            log.warn("Running without auth_token; only public guest access will be available.")

        target_dir = download_dir / "twt" / username_or_id
        for folder in ("posts", "images", "videos"):
            path.mkdir(target_dir / folder)

        log.info("Starting download for: {target}", target=username_or_id)
        log.info("Destination: {path}", path=target_dir)

        # The API client sends the session headers with every request, media included. Files found
        # through the browser fallback were always fetched with a plain client.
        with http.Client(headers=self._get_headers(cookie_map), timeout=20.0) as client, \
                http.Client() as plain_client, \
                Downloads(self.code, target, download_dir, limit) as self.files:
            # Check if target is a numeric ID (single tweet)
            if username_or_id.isdigit() and len(username_or_id) > 10:
                self._download_single_tweet(username_or_id, target_dir, client)
                finished = False
            else:
                # Otherwise, treat as username and download user posts & media timeline
                finished = self._download_user_timeline(
                    username_or_id, target_dir, headless, client, plain_client
                )

        if finished:
            log.ok("Finished @{name}: {n} files", name=username_or_id, n=self.files.saved)
        return self.files.saved

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
            "user-agent": http.agents.chrome,
        }

    def _download_user_timeline(
        self,
        screen_name: str,
        target_dir: Path,
        headless: bool,
        client: httpx.Client,
        plain_client: httpx.Client,
    ) -> bool:
        """Fetch user posts and media timeline using Twitter GraphQL API with Playwright fallback.

        Returns True when the API listed every timeline, False when the browser took over.
        """
        # 1. Resolve user ID from screen name
        user_id = self._resolve_user_id(client, screen_name)
        if not user_id:
            log.warn("Direct API resolution failed for '{name}', attempting Playwright browser...", name=screen_name)
            self._download_via_playwright(screen_name, target_dir, headless, plain_client)
            return False

        log.info("Resolved @{name} -> User ID: {id}", name=screen_name, id=user_id)

        # 2. Iterate through user timelines: Posts then Media
        timelines = [
            ("posts", QUERY_USER_ORIGINALS_TIMELINE),
            ("media", QUERY_USER_MEDIA),
        ]

        for category, query_id in timelines:
            if self.files.full:
                break

            log.info("Fetching {category} timeline for @{name}...", category=category, name=screen_name)
            cursor = None
            empty_page_count = 0

            while not self.files.full:
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
                    data = client.json(url, params=params, follow_redirects=False)
                except httpx.HTTPStatusError as e:
                    log.warn(
                        "API status {status} on {category}, switching to browser extraction...",
                        status=e.response.status_code, category=category,
                    )
                    self._download_via_playwright(screen_name, target_dir, headless, plain_client)
                    return False
                except Exception as e:
                    log.error("API request error: {err}", err=e)
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
                    self._save_media_item(item, target_dir, client)

                if not next_cursor or next_cursor == cursor or next_cursor.startswith("0|"):
                    break
                cursor = next_cursor

        return True

    def _resolve_user_id(self, client: httpx.Client, screen_name: str) -> Optional[str]:
        """Query UserByScreenName GraphQL endpoint to obtain numeric rest_id."""
        for query in [QUERY_USER_BY_SCREEN_NAME_PRIMARY, QUERY_USER_BY_SCREEN_NAME_LEGACY]:
            url = f"https://x.com/i/api/graphql/{query}"
            params = {
                "variables": json.dumps({"screen_name": screen_name, "withSafetyModeUserFields": True}),
                "features": json.dumps({"hidden_profile_likes_enabled": True}),
            }
            try:
                data = client.json(url, params=params, follow_redirects=False)
                rest_id = scrape.dig(data, "data.user.result.rest_id")
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
            user_result = scrape.dig(data, "data.user.result", default={})
            timeline_obj = scrape.dig(
                user_result, "timeline_v2.timeline || timeline.timeline || timeline", default={}
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
                            tc = scrape.dig(grid_item, "item.itemContent || itemContent", default={})
                            raw_items.append(tc)
                    elif "itemContent" in content:
                        raw_items.append(content["itemContent"])

                    for item_content in raw_items:
                        tweet_result = scrape.dig(item_content, "tweet_results.result", default={})
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
                        entities = scrape.dig(legacy, "extended_entities.media", default=[])
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
                                variants = scrape.dig(media, "video_info.variants", default=[])
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
            log.warn("Error parsing timeline JSON: {err}", err=e)

        return media_items, next_cursor

    def _save_media_item(self, item: Dict[str, Any], target_dir: Path, client: httpx.Client) -> bool:
        """Queue an extracted Twitter media item into the platform-optimized layout."""
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

        # Check deduplication across all subfolders for this user to avoid re-downloading duplicate
        # files, counting files that are still downloading.
        claimed = self.files.claimed if self.files else set()
        for check_folder in [target_dir / "posts", target_dir / "images", target_dir / "videos"]:
            existing = check_folder / filename
            if (existing.is_file() and existing.stat().st_size > 0) or existing in claimed:
                log.info("skip {name}", name=f"{dest_folder.name}/{filename}")
                return False

        return self.files.add(client, item["url"], dest_path, key=f"{tweet_id}_{idx}")

    def _download_single_tweet(self, tweet_id: str, target_dir: Path, client: httpx.Client) -> None:
        """Queue media for a single status / tweet ID."""
        url = f"https://x.com/i/api/graphql/{QUERY_TWEET_DETAIL}"
        params = {
            "variables": json.dumps({"tweetId": tweet_id, "withCommunity": False, "includePromotedContent": False}),
            "features": json.dumps({"responsive_web_graphql_timeline_navigation_enabled": True}),
        }

        try:
            data = client.json(url, params=params, follow_redirects=False)
        except httpx.HTTPStatusError as e:
            log.error("HTTP {status} fetching tweet {id}", status=e.response.status_code, id=tweet_id)
            return

        # Wrap in structure compatible with timeline parser
        tweet_result = scrape.dig(data, "data.tweetResult.result", default={})
        fake_timeline = {"data": {"user": {"result": {"timeline_v2": {"timeline": {"instructions": [
            {"entries": [{"content": {"itemContent": {"tweet_results": {"result": tweet_result}}}}]}
        ]}}}}}}

        items, _ = self._parse_timeline_entries(fake_timeline)
        for item in items:
            item["category"] = "posts"
            self._save_media_item(item, target_dir, client)

    def _download_via_playwright(
        self,
        screen_name: str,
        target_dir: Path,
        headless: bool,
        client: httpx.Client,
    ) -> None:
        """Playwright fallback: navigates to x.com/<user> and x.com/<user>/media, scrolling to the end and intercepting GraphQL replies."""
        cookies_list = load_cookies(self.code)
        log.info("Launching Playwright Chromium for @{name}...", name=screen_name)
        captured = [0]
        current_category = ["posts"]

        tabs = [
            ("posts", f"https://x.com/{screen_name}"),
            ("media", f"https://x.com/{screen_name}/media"),
        ]

        def on_timeline(response: Any, data: Any) -> None:
            items, _ = self._parse_timeline_entries(data)
            captured[0] += len(items)
            for it in items:
                it["category"] = current_category[0]
                self._save_media_item(it, target_dir, client)

        # This browser never hid navigator.webdriver, so stealth stays off.
        with browser.open(headless=headless, user_agent=http.agents.chrome, cookies=cookies_list, stealth=False) as page:
            browser.capture(page, is_timeline, on_timeline)

            for cat, tab_url in tabs:
                if self.files.full:
                    break
                current_category[0] = cat
                log.info("Navigating tab {tab}: {url}", tab=cat, url=tab_url)
                try:
                    page.goto(tab_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3000)

                    # Continuously scroll to the end of the tab
                    scroll(page, cat, count=lambda: captured[0], more=lambda: not self.files.full)
                except Exception as e:
                    log.warn("Browser navigation error on {url}: {err}", url=tab_url, err=e)

        log.info("Captured {n} items from browser session.", n=captured[0])
