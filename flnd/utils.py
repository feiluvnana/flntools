"""
Utility functions for file downloading, progress reporting, and deduplication.
"""

import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional
import httpx


def sanitize_name(name: str) -> str:
    """Sanitize strings to be safe for directory and file names."""
    # Replace characters that are invalid in Windows and Unix paths
    sanitized = re.sub(r'[\\/*?:"<>|]', "_", name)
    # Collapse multiple underscores and spaces
    sanitized = re.sub(r'[\s_]+', "_", sanitized).strip("._ ")
    return sanitized or "untitled"


def format_bytes(num_bytes: int) -> str:
    """Format byte counts into human-readable strings (KB, MB, GB)."""
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"


def download_file(
    url: str,
    dest_path: Path,
    headers: Optional[Dict[str, str]] = None,
    client: Optional[httpx.Client] = None,
) -> bool:
    """
    Download a remote file to `dest_path`.

    Features:
      - Deduplication: If `dest_path` already exists and has size > 0, skips downloading.
      - Atomic write: Writes to a temporary file (`.part`) and renames on completion.
      - Parent directories are automatically created.

    Returns:
      True if the file was downloaded, False if skipped (already exists) or failed.
    """
    # 1. Deduplication check
    if dest_path.is_file() and dest_path.stat().st_size > 0:
        print(f"  [skip] Already downloaded: {dest_path.name}")
        return False

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_suffix(dest_path.suffix + ".part")

    # Use provided client or create a short-lived one
    owns_client = client is None
    http = client or httpx.Client(timeout=30.0, follow_redirects=True)

    default_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
    }
    if headers:
        default_headers.update(headers)

    try:
        with http.stream("GET", url, headers=default_headers, follow_redirects=True) as response:
            if response.status_code not in (200, 206):
                print(f"  [fail] HTTP {response.status_code} for URL: {url[:80]}...")
                return False

            total_bytes = 0
            with open(temp_path, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        total_bytes += len(chunk)

        # Atomic rename once write is successful
        temp_path.replace(dest_path)
        print(f"  [saved] {dest_path.parent.name}/{dest_path.name} ({format_bytes(total_bytes)})")
        return True

    except Exception as e:
        print(f"  [error] Failed to download {url[:60]}: {e}")
        if temp_path.exists():
            temp_path.unlink()
        return False

    finally:
        if owns_client:
            http.close()


def scroll_page_to_end(
    page: Any,
    get_count: Optional[Callable[[], int]] = None,
    limit: Optional[int] = None,
    max_idle_rounds: int = 4,
    step_delay_ms: int = 2000,
    max_scrolls: int = 200,
    check_has_more: Optional[Callable[[], bool]] = None,
    custom_scroll_action: Optional[Callable[[Any], None]] = None,
) -> None:
    """
    Continuously scrolls a Playwright page until the end of the feed/content is reached.

    Args:
        page: Playwright Page instance.
        get_count: Optional callable returning current count of collected items.
        limit: Max items to collect before breaking early.
        max_idle_rounds: Number of consecutive unchanged scrolls before stopping.
        step_delay_ms: Milliseconds to wait between scrolls.
        max_scrolls: Safety cap on total scroll steps.
        check_has_more: Optional callable returning False when the platform explicitly indicates no more items.
        custom_scroll_action: Optional custom scroll callable (e.g. for TikTok's scroll dispatch trick).
    """
    prev_height = 0
    prev_count = get_count() if get_count else 0
    idle_rounds = 0

    for scroll_idx in range(max_scrolls):
        if limit and get_count and get_count() >= limit:
            break

        if check_has_more and not check_has_more():
            break

        if custom_scroll_action:
            try:
                custom_scroll_action(page)
            except Exception:
                pass
        else:
            try:
                page.mouse.wheel(0, 2000)
            except Exception:
                pass

        try:
            page.wait_for_timeout(step_delay_ms)
        except Exception:
            break

        try:
            new_height = page.evaluate("() => { window.scrollBy(0, 500); return document.body ? document.body.scrollHeight : 0; }")
        except Exception:
            break

        current_count = get_count() if get_count else 0

        if new_height > prev_height or current_count > prev_count:
            idle_rounds = 0
            prev_height = max(prev_height, new_height)
            prev_count = current_count
        else:
            idle_rounds += 1
            # Nudge scroll up slightly to re-trigger intersection observers / event listeners
            try:
                page.evaluate("() => { window.scrollBy(0, -350); }")
                page.wait_for_timeout(800)
                page.evaluate("() => { window.scrollTo(0, document.body ? document.body.scrollHeight : 0); window.dispatchEvent(new Event('scroll')); }")
            except Exception:
                pass

            if idle_rounds >= max_idle_rounds:
                break

