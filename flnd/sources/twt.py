"""
Twitter / X source for flnd.

  - The user's Posts timeline goes to posts/, and the Media timeline's photos to images/ and
    videos to videos/. A file already saved from one timeline is not saved again from the other.
  - Photos are full resolution (?name=orig), and videos the highest-bitrate MP4.
  - Each timeline is read to its end with cursor pagination. When the API refuses, a browser
    scrolls the profile instead and reads the same GraphQL responses.

Folder layout, under `.flnd/downloads/twt/<username>/`:
  posts/<YYYY-MM-DD>_<tweet_id>_<idx>.jpg (or .mp4)
  images/<YYYY-MM-DD>_<tweet_id>_<idx>.jpg
  videos/<YYYY-MM-DD>_<tweet_id>_<idx>.mp4
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Optional

from flnd.session import Session
from flnd.sources.base import FetchError, Source
from flnd.target import Target, TargetError, link, name, unreadable
from shared import selector

# The user agent Twitter's API has always been sent: a desktop Chrome.
CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/128.0.0.0 Safari/537.36")

# Twitter's standard web client public bearer token
TWITTER_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# GraphQL query IDs used by Twitter Web
QUERY_USER_BY_SCREEN_NAME_PRIMARY = "AMIBMjtxEEATh4z8V9GtRg/UserByScreenName"
QUERY_USER_BY_SCREEN_NAME_LEGACY = "ck5KkZ8t5cOmoLssopN99Q/UserByScreenName"
QUERY_USER_ORIGINALS_TIMELINE = "ZFKu1DFS63hgrkLzR3Hsdw/UserOriginalsTimeline"
QUERY_USER_MEDIA = "jCRhbOzdgOHp6u9H4g2tEg/UserMedia"

TIMELINE_ENDPOINTS = ["UserMedia", "UserOriginalsTimeline", "UserTweets", "UserTweetsAndReplies"]
# Paths on x.com that are pages of the site, not profiles.
NOT_PROFILES = {"home", "explore", "search", "i", "settings", "notifications", "messages", "hashtag"}


def is_timeline(response: Any) -> bool:
    return "graphql" in response.url and any(endpoint in response.url for endpoint in TIMELINE_ENDPOINTS)


def timeline_params(user_id: str, category: str, cursor: Optional[str]) -> Dict[str, str]:
    """The query of one timeline page: its variables and features, as JSON."""
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
    return {"variables": json.dumps(variables), "features": json.dumps(features)}


class Twitter(Source):
    """Twitter / X users, from the GraphQL API, with a browser when the API refuses."""

    code = "twt"
    name = "Twitter/X"
    domains = ("x.com", "twitter.com")
    examples = ("twt:example_handle", "twt:@example_handle", "twt:44196397", "https://x.com/example_handle")
    needs = ("auth_token",)
    categories = ("posts", "media")
    default_categories = ("posts", "media")
    user_agent = CHROME
    # This browser never hid navigator.webdriver, so stealth stays off.
    stealth = False

    def headers(self, cookies: Mapping[str, str]) -> Mapping[str, str]:
        """Twitter API headers: the bearer token, CSRF, and the session's cookies."""
        return {
            "authorization": TWITTER_BEARER,
            "x-csrf-token": cookies.get("ct0", ""),
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session" if "auth_token" in cookies else "",
            "x-twitter-client-language": "en",
            "cookie": "; ".join([f"{k}={v}" for k, v in cookies.items()]),
        }

    def resolve(self, text: str) -> Target:
        """A screen name, or a number, which is a user id."""
        found = link(text, self.domains)
        if found is None:
            user = name(text)
        elif len(found.parts) >= 2 and found.parts[1] == "status":
            raise TargetError(f"{self.code}: single posts aren't supported yet; "
                              f"give the profile, for example {self.examples[0]}")
        else:
            user = name(found.parts[0]) if found.parts and found.parts[0] not in NOT_PROFILES else None
        if not user:
            raise unreadable(self, text)
        return Target(self.code, "user", user)

    def run(self, session: Session) -> None:
        """Fetch user posts and media timeline using Twitter GraphQL API with a browser fallback."""
        screen_name = session.target.value
        # 1. A number is a user id already; a name is looked up.
        user_id = screen_name if screen_name.isdigit() else self._resolve_user_id(session, screen_name)
        if not user_id:
            session.warn("Direct API resolution failed for '{name}', attempting the browser...", name=screen_name)
            self._download_via_browser(session, screen_name)
            return

        session.info("Resolved @{name} -> User ID: {id}", name=screen_name, id=user_id)

        # 2. Iterate through user timelines: Posts then Media
        timelines = [
            ("posts", QUERY_USER_ORIGINALS_TIMELINE),
            ("media", QUERY_USER_MEDIA),
        ]

        for category, query_id in timelines:
            if session.full:
                break
            if not session.wants(category=category):
                continue
            session.info("Fetching {category} timeline for @{name}...", category=category, name=screen_name)
            if not self._read_timeline(session, user_id, category, query_id):
                self._download_via_browser(session, screen_name)
                return

    def _read_timeline(self, session: Session, user_id: str, category: str, query_id: str) -> bool:
        """Read one timeline to its end; False when the API refused it and the browser should take over."""
        cursor = None
        empty_page_count = 0

        while not session.full:
            url = f"https://x.com/i/api/graphql/{query_id}"
            try:
                data = session.fetch_json(url, params=timeline_params(user_id, category, cursor),
                                          follow_redirects=False)
            except FetchError as e:
                if e.status is None:
                    session.warn("API request error: {err}", err=e)
                    return True
                session.warn("API status {status} on {category}, switching to browser extraction...",
                             status=e.status, category=category)
                return False

            # Extract media items from timeline instructions
            items, next_cursor = self._parse_timeline_entries(session, data)
            if not items:
                empty_page_count += 1
                if empty_page_count >= 5:
                    break
            else:
                empty_page_count = 0

            for item in items:
                item["category"] = category
                self._save_media_item(session, item)

            if not next_cursor or next_cursor == cursor or next_cursor.startswith("0|"):
                break
            cursor = next_cursor
        return True

    def _resolve_user_id(self, session: Session, screen_name: str) -> Optional[str]:
        """Query UserByScreenName GraphQL endpoint to obtain numeric rest_id."""
        for query in [QUERY_USER_BY_SCREEN_NAME_PRIMARY, QUERY_USER_BY_SCREEN_NAME_LEGACY]:
            url = f"https://x.com/i/api/graphql/{query}"
            params = {
                "variables": json.dumps({"screen_name": screen_name, "withSafetyModeUserFields": True}),
                "features": json.dumps({"hidden_profile_likes_enabled": True}),
            }
            try:
                data = session.fetch_json(url, params=params, follow_redirects=False)
                rest_id = selector.select(data).jmespath("data.user.result.rest_id").first
                if rest_id:
                    return rest_id
            except Exception:
                pass
        return None

    def _parse_timeline_entries(self, session: Session,
                                data: Dict[str, Any]) -> tuple[List[Dict[str, Any]], Optional[str]]:
        """Parse tweets and pagination cursor from GraphQL timeline JSON response."""
        media_items = []
        next_cursor = None

        try:
            user_result = (selector.select(data).jmespath("data.user.result").first or {})
            timeline_obj = (selector.select(user_result).jmespath(
                "timeline_v2.timeline"
                " || timeline.timeline"
                " || timeline"
            ).first or {})
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
                            tc = (selector.select(grid_item).jmespath("item.itemContent || itemContent").first or {})
                            raw_items.append(tc)
                    elif "itemContent" in content:
                        raw_items.append(content["itemContent"])

                    for item_content in raw_items:
                        tweet_result = (selector.select(item_content).jmespath("tweet_results.result").first or {})
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
                        entities = selector.select(legacy).jmespath("extended_entities.media").all
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
                                variants = selector.select(media).jmespath("video_info.variants").all
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
            session.warn("Error parsing timeline JSON: {err}", err=e)

        return media_items, next_cursor

    def _save_media_item(self, session: Session, item: Dict[str, Any]) -> bool:
        """Queue an extracted Twitter media item into the platform-optimized layout.

        The key is the tweet and the media's place in it, so the same file listed by both
        timelines is saved once.
        """
        category = item.get("category", "media")
        kind = item["kind"]
        tweet_id = item["tweet_id"]
        date_str = item.get("date") or ""
        idx = item.get("idx", 0)
        ext = "jpg" if kind == "image" else "mp4"

        filename = f"{date_str}_{tweet_id}_{idx}.{ext}" if date_str else f"{tweet_id}_{idx}.{ext}"
        if category == "posts":
            dest_folder = "posts"
        else:
            dest_folder = "images" if kind == "image" else "videos"
        return session.save(item["url"], filename, category=category,
                            date=date.fromisoformat(date_str) if date_str else None, folder=dest_folder,
                            key=f"{tweet_id}_{idx}")

    def _download_via_browser(self, session: Session, screen_name: str) -> None:
        """Browser fallback: x.com/<user> and x.com/<user>/media, scrolled to the end, reading GraphQL replies."""
        session.info("Launching the browser for @{name}...", name=screen_name)
        captured = [0]
        profile = f"https://x.com/i/user/{screen_name}" if screen_name.isdigit() else f"https://x.com/{screen_name}"
        tabs = [
            ("posts", profile),
            ("media", f"{profile}/media"),
        ]

        def on_timeline(session: Session, data: Any, response: Any) -> None:
            if session.folder is None:
                return
            items, _ = self._parse_timeline_entries(session, data)
            captured[0] += len(items)
            for it in items:
                it["category"] = session.folder
                self._save_media_item(session, it)

        session.on_response(is_timeline, on_timeline)
        for cat, tab_url in tabs:
            if session.full:
                break
            if not session.wants(category=cat):
                continue
            session.folder = cat
            session.info("Navigating tab {tab}: {url}", tab=cat, url=tab_url)
            # Continuously scroll to the end of the tab
            if session.goto(tab_url):
                session.scroll(count=lambda: captured[0])
        session.folder = None

        session.info("Captured {n} items from browser session.", n=captured[0])
