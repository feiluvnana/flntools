"""
Instagram source for flnd.

  - Active 24h stories come from the mobile API tray, when the login has `ds_user_id`.
  - Profile tabs are read in a browser: posts, reels, tagged and reposts, then highlights.
  - Carousels, reels and stories are all saved, from GraphQL responses.

Folder layout, under `.flnd/downloads/ins/<username>/`:
  posts/, tagged/, reposts/   <YYYY-MM-DD>_<post_id>_<idx>.jpg (or .mp4)
  reels/                      <YYYY-MM-DD>_<reel_id>.mp4
  stories/                    <YYYY-MM-DD>_<story_id>.jpg (or .mp4)
  highlights/<title>/         <YYYY-MM-DD>_<story_id>.jpg (or .mp4)
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Optional

from flnd.session import Session
from flnd.sources.base import Source
from flnd.target import Target, TargetError, link, name, unreadable
from shared import fmt, selector

APP_ID = "936619743392459"
# Mobile Android User-Agent: Instagram story tray API refuses desktop user-agents
INSTAGRAM_MOBILE_UA = (
    "Instagram 219.0.0.12.117 Android (33/13; 420dpi; 1080x2400; samsung; SM-G991B; o1s; exynos2100; en_US; 346132297)"
)


# Tabs to navigate in order: (folder_name, relative_path)
TABS = [
    ("posts", ""),
    ("reels", "reels/"),
    ("tagged", "tagged/"),
    ("reposts", "reposts/"),
]
# The first part of a URL that points to one post, not a profile.
SINGLE_POSTS = {"p", "reel", "reels", "tv", "stories"}


def is_profile_api(response: Any) -> bool:
    return "graphql" in response.url or "/api/v1/" in response.url


def taken_on(day: str) -> Optional[date]:
    return date.fromisoformat(day) if day else None


class Instagram(Source):
    """Instagram profiles: stories from the mobile API, everything else from the browser."""

    code = "ins"
    name = "Instagram"
    domains = ("instagram.com",)
    examples = ("ins:example_user", "ins:@example_user", "https://www.instagram.com/example_user/")
    needs = ("sessionid",)
    categories = ("posts", "reels", "tagged", "reposts", "stories", "highlights")
    default_categories = ("posts", "reels", "stories", "highlights")

    def headers(self, cookies: Mapping[str, str]) -> Mapping[str, str]:
        return {"x-ig-app-id": APP_ID}

    def resolve(self, text: str) -> Target:
        found = link(text, self.domains)
        if found is None:
            user = name(text)
        elif found.parts[:1] and found.parts[0] in SINGLE_POSTS:
            raise TargetError(f"{self.code}: single posts aren't supported yet; "
                              f"give the profile, for example {self.examples[0]}")
        else:
            user = name(found.parts[0]) if found.parts else None
        if not user:
            raise unreadable(self, text)
        return Target(self.code, "user", user)

    def run(self, session: Session) -> None:
        username = session.target.value
        # 1. Fetch active 24h stories via mobile API tray
        user_id = session.cookies.get("ds_user_id")
        if user_id and session.wants(category="stories"):
            session.info("Checking active 24h stories tray...")
            self._download_active_stories(session, user_id)

        # 2. Navigate profile tabs in the browser: posts, reels, tagged, reposts, highlights
        in_browser = [name for name, _ in TABS] + ["highlights"]
        if not session.full and any(session.wants(category=category) for category in in_browser):
            self._download_via_browser(session, username)

    def _download_active_stories(self, session: Session, user_id: str) -> None:
        """Queue active user stories from Instagram's mobile reels_media API."""
        cookie_str = "; ".join([f"{k}={v}" for k, v in session.cookies.items()])
        headers = {
            "user-agent": INSTAGRAM_MOBILE_UA,
            "cookie": cookie_str,
            "x-ig-app-id": APP_ID,
        }

        url = "https://i.instagram.com/api/v1/feed/reels_media/"
        params = {"user_ids": json.dumps([user_id]), "source": "profile"}

        try:
            data = session.fetch_json(url, headers=headers, params=params, follow_redirects=False, soft=True)
        except Exception as e:
            session.warn("Could not fetch stories from mobile API: {err}", err=e)
            return
        if data is None:
            return
        reels = data.get("reels_media", []) or list(data.get("reels", {}).values())
        queued = 0

        for reel in reels:
            items = reel.get("items", [])
            for item in items:
                story_id = str(item.get("id") or item.get("pk") or "").split("_")[0]
                ts = item.get("taken_at")
                taken = datetime.fromtimestamp(ts) if ts else datetime.now()
                date_str = taken.strftime("%Y-%m-%d")
                posted = taken.date() if ts else None

                # Check for video first, then highest quality image
                if item.get("media_type") == 2 and item.get("video_versions"):
                    best_video = item["video_versions"][0]["url"]
                    queued += session.save(best_video, f"{date_str}_{story_id}.mp4", category="stories",
                                           date=posted, folder="stories")
                elif selector.select(item).jmespath("image_versions2.candidates").first:
                    best_image = item["image_versions2"]["candidates"][0]["url"]
                    queued += session.save(best_image, f"{date_str}_{story_id}.jpg", category="stories",
                                           date=posted, folder="stories")

        if queued:
            session.info("Found {n} active stories in the mobile tray.", n=queued)

    def _download_via_browser(self, session: Session, username: str) -> None:
        """
        Use the browser to navigate all user profile tabs:
          - / (Posts)
          - /reels/ (Reels)
          - /tagged/ (Tagged posts)
          - /reposts/ (Reposts, if enabled)
          - Highlights collections
        Intercepts GraphQL network responses and queues media into platform-optimized subfolders.
        """
        captured_highlights: List[Dict[str, Any]] = []
        found = {name: 0 for name, _ in TABS}
        headers = {"Referer": f"https://www.instagram.com/{username}/"}

        def on_profile(session: Session, data: Any, response: Any) -> None:
            # Extract highlights if available on profile response
            for h in self._extract_highlights_from_json(data):
                if h not in captured_highlights:
                    captured_highlights.append(h)

            # Extract media items based on current active tab; None once the tabs are done.
            tab = session.folder
            if tab is None:
                return
            extracted = self._extract_media_from_json(session, data, tab=tab)
            found[tab] += len(extracted)
            for item in extracted:
                self._queue_media(session, item, tab, headers, sniff_video=True)

        session.on_response(is_profile_api, on_profile)
        # Highlights are listed by the profile page, so it is loaded even when no tab is wanted.
        if session.wants(category="highlights") and not session.wants(category="posts"):
            session.folder = None
            session.goto(f"https://www.instagram.com/{username}/")

        # Navigate through each tab
        for tab_name, tab_path in TABS:
            if session.full:
                break
            if not session.wants(category=tab_name):
                continue

            session.folder = tab_name
            tab_url = f"https://www.instagram.com/{username}/{tab_path}"
            session.info("Navigating tab {tab}: {url}", tab=tab_name, url=tab_url)

            # Continuously scroll down to the end to trigger all GraphQL replies
            if session.goto(tab_url):
                session.scroll(count=lambda: found[tab_name])

            session.info("{tab}: intercepted {n} media items.", tab=tab_name, n=found[tab_name])

        # Media seen after the tabs was never downloaded; only highlights are still collected.
        session.folder = None

        # 3. Download highlights if found on profile
        if captured_highlights and not session.full and session.wants(category="highlights"):
            self._download_highlights(session, username, captured_highlights)

    def _download_highlights(self, session: Session, username: str, highlights: List[Dict[str, Any]]) -> None:
        """Open each highlight from the profile page, where its element can be clicked."""
        session.info("Found {n} highlight collections.", n=len(highlights))
        if not session.page.url.rstrip("/").endswith(f"instagram.com/{username}"):
            session.goto(f"https://www.instagram.com/{username}/")
        for hl in highlights:
            if session.full:
                break
            self._download_highlight(session, hl)

    def _download_highlight(self, session: Session, hl: Dict[str, Any]) -> None:
        """Open one highlight and queue the stories it loads."""
        hl_id = hl["id"].replace("highlight:", "")
        hl_title = hl["title"]
        hl_folder = f"highlights/{fmt.filename(hl_title)}"
        hl_url = f"https://www.instagram.com/stories/highlights/{hl_id}/"

        session.info("Opening highlight '{title}': {url}", title=hl_title, url=hl_url)
        found = [0]

        def on_highlight(session: Session, data: Any, response: Any) -> None:
            parsed = self._extract_stories_from_json(data)
            found[0] += len(parsed)
            for s_item in parsed:
                self._queue_media(session, s_item, hl_folder, None, sniff_video=False, category="highlights")

        stop = session.on_response(is_profile_api, on_highlight)

        # Click highlight element on profile page to trigger client-side Relay query
        page = session.page.raw
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
                session.goto(hl_url)
                page.wait_for_timeout(3500)
        except Exception as e:
            session.warn("Could not open highlight '{title}': {err}", title=hl_title, err=e)
        stop()

        session.info("Highlight '{title}': intercepted {n} items.", title=hl_title, n=found[0])

    def _queue_media(
        self,
        session: Session,
        item: Dict[str, Any],
        folder: str,
        headers: Optional[Dict[str, str]],
        sniff_video: bool,
        category: Optional[str] = None,
    ) -> None:
        """Queue each file of a post or story. Posts also count a `.mp4` URL as a video."""
        date_str = item.get("date") or ""
        post_id = item["id"]

        for idx, media_url in enumerate(item.get("media_urls", [])):
            is_video = item.get("is_video", False) or (sniff_video and ".mp4" in media_url)
            ext = "mp4" if is_video else "jpg"
            stem = f"{post_id}_{idx}" if len(item["media_urls"]) > 1 else post_id
            filename = f"{date_str}_{stem}.{ext}" if date_str else f"{stem}.{ext}"
            session.save(media_url, filename, category=category or folder, date=taken_on(date_str),
                         folder=folder, headers=headers)

    def _extract_media_from_json(self, session: Session, data: Dict[str, Any],
                                 tab: str = "posts") -> List[Dict[str, Any]]:
        """
        Extract media items from Instagram GraphQL JSON for posts, reels, tagged, or reposts.
        """
        extracted = []
        try:
            edges = []

            # 1. Main timeline posts
            if tab == "posts":
                timeline = (selector.select(data).jmespath(
                    "data.xdt_api__v1__feed__user_timeline_graphql_connection"
                    " || data.user.edge_owner_to_timeline_media"
                ).first or {})
                edges = timeline.get("edges", [])

            # 2. Reels tab
            elif tab == "reels":
                clips = (selector.select(data).jmespath(
                    "data.fetch__XDTUserDict.clips_connection"
                    " || data.xdt_api__v1__clips__user__connection_v2"
                    " || data.user.edge_felix_video_timeline"
                ).first or {})
                edges = clips.get("edges", [])

            # 3. Tagged tab
            elif tab == "tagged":
                tagged_conn = (selector.select(data).jmespath(
                    "data.xdt_api__v1__usertags__user_id__feed_connection"
                    " || data.user.edge_user_to_photos_of_you"
                ).first or {})
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
                reposts_conn = (selector.select(data).jmespath(
                    "data.fetch__XDTUserDict.user_reposts_timeline"
                    " || data.xdt_api__v1__feed__user_reposts_graphql_connection"
                    " || data.user_reposts_timeline"
                ).first or {})
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
                        images = selector.select(child).jmespath("image_versions2.candidates").all
                        if videos:
                            media_urls.append(videos[0]["url"])
                            is_video = True
                        elif images:
                            media_urls.append(images[0]["url"])

                # Legacy edge_sidecar_to_children
                elif selector.select(node).jmespath("edge_sidecar_to_children.edges").first:
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
                    images = selector.select(node).jmespath("image_versions2.candidates").all
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
            session.warn("Error parsing Instagram JSON for {tab}: {err}", tab=tab, err=e)

        return extracted

    def _extract_highlights_from_json(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract highlight collections from profile GraphQL."""
        highlights = []
        try:
            edges = selector.select(data).jmespath("data.highlights.edges").all
            for edge in edges:
                node = edge.get("node", {})
                hid = node.get("id")
                title = fmt.filename(node.get("title", "highlight"))
                if hid:
                    highlights.append({"id": hid, "title": title})
        except Exception:
            pass
        return highlights

    def _extract_stories_from_json(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract story/highlight media items from GraphQL connection."""
        items = []
        try:
            nodes = selector.select(data).jmespath(
                "data.xdt_api__v1__feed__reels_media__connection.edges"
                " || data.reels_media"
            ).all
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
                    elif selector.select(item).jmespath("image_versions2.candidates").first:
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
