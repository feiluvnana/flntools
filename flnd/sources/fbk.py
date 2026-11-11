"""Facebook: a profile's posts, photos, tagged photos, albums, reels and videos, read in a browser.

Each profile tab is opened and scrolled, and the Comet GraphQL responses it triggers are
read for photos (largest image) and videos (HD progressive URL first), with their real dates.

Folder layout, under `.flnd/downloads/fbk/<username_or_id>/`:
  posts/, photos/, tagged/, reels/, videos/   <YYYY-MM-DD>_<id>.jpg (or .mp4)
  albums/<album_title>/                        <YYYY-MM-DD>_<photo_id>.jpg
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from flnd.session import Session
from flnd.sources.base import Source
from flnd.target import Target, TargetError, link, name, unreadable
from shared import fmt, selector

# Tabs to navigate in order: (folder_name, relative_url_path)
TABS = [
    ("posts", ""),
    ("photos", "photos"),
    ("tagged", "photos_of"),
    ("albums", "photos_albums"),
    ("reels", "reels"),
    ("videos", "videos"),
]
# Paths on facebook.com that are pages of the site, not profiles.
NOT_PROFILES = {"watch", "reel", "reels", "videos", "photo", "photos", "groups", "events", "stories", "story.php"}
TAB_PATHS = {path for _, path in TABS if path}


def is_profile(parts: List[str]) -> bool:
    """Whether a facebook.com path is a profile or one of its tabs, such as `example.page/photos`."""
    return parts[0] not in NOT_PROFILES and (len(parts) == 1 or parts[1] in TAB_PATHS)


class Facebook(Source):
    """Facebook profiles, read in a browser that is logged in with the saved cookies."""

    code = "fbk"
    name = "Facebook"
    domains = ("facebook.com", "fb.com", "fb.watch")
    short_links = ("fb.watch",)
    examples = ("fbk:example.page", "fbk:100012345678901", "https://www.facebook.com/profile.php?id=100012345678901")
    needs = ("c_user", "xs")
    categories = tuple(tab for tab, _ in TABS)
    default_categories = ("posts", "photos", "albums", "reels", "videos")

    def resolve(self, text: str) -> Target:
        found = link(text, self.domains)
        if found is None:
            user = name(text)
        elif found.parts[:1] == ["profile.php"]:
            user = found.query.get("id")
        elif found.parts and is_profile(found.parts):
            user = name(found.parts[0])
        elif found.parts:
            raise TargetError(f"{self.code}: single posts and videos aren't supported yet; "
                              f"give the profile, for example {self.examples[0]}")
        else:
            user = None
        if not user:
            raise unreadable(self, text)
        return Target(self.code, "user", user)

    def run(self, session: Session) -> None:
        user = session.target.value
        found = {tab: 0 for tab, _ in TABS}
        headers = {"Referer": f"https://www.facebook.com/{user}"}

        def on_graphql(session: Session, data: Any, response: Any) -> None:
            # Facebook sends one JSON object per line; other lines were never read.
            tab = session.folder
            if not isinstance(data, dict) or tab is None:
                return
            items = self._parse_facebook_graphql(data, default_category=tab)
            found[tab] += len(items)
            for item in items:
                self._queue_item(session, item, tab, headers)

        session.on_response("graphql", on_graphql, lines=True)
        for tab_name, tab_path in TABS:
            if session.full:
                break
            if not session.wants(category=tab_name):
                continue
            session.folder = tab_name
            tab_url = f"https://www.facebook.com/{user}/{tab_path}".rstrip("/")
            session.info("Navigating tab {tab}: {url}", tab=tab_name, url=tab_url)
            if session.goto(tab_url):
                session.scroll(count=lambda: found[tab_name])
            session.info("{tab}: intercepted {n} media items.", tab=tab_name, n=found[tab_name])
        session.folder = None

    def _queue_item(self, session: Session, item: Dict[str, Any], tab_name: str, headers: Dict[str, str]) -> None:
        category = item.get("category", tab_name)
        album_title = item.get("album")
        date_str = item.get("date", "").strip()
        item_id = item["id"]
        kind = item["kind"]
        ext = "mp4" if kind == "video" else "jpg"

        # Determine destination subfolder
        if album_title:
            dest_folder = f"albums/{fmt.filename(album_title)}"
        elif category == "reels":
            dest_folder = "reels"
        elif category == "videos" or kind == "video":
            dest_folder = "videos"
        elif category == "tagged":
            dest_folder = "tagged"
        elif category == "photos":
            dest_folder = "photos"
        else:
            dest_folder = "posts"

        # Do NOT default to today's date when date is unknown; format cleanly
        filename = f"{date_str}_{item_id}.{ext}" if date_str else f"{item_id}.{ext}"
        # The category is the tab it was found on, wherever its folder is: tagged photos are opt-in.
        session.save(item["url"], filename, category=tab_name, date=date.fromisoformat(date_str) if date_str else None,
                     folder=dest_folder, headers=headers)

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
