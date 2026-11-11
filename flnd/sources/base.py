"""
Base class for all source extractors.

Each source has full autonomy over:
  - Loading its cookies
  - Choosing its scraping mechanism (HTTPX API, Playwright, or hybrid)
  - Extracting media and platform-specific features (posts, reels, stories, highlights, albums)
  - Organizing files in `.flnd/downloads/<source>/<user>/<subfolder>/`
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Tuple

from flnd.target import Target


class BaseSource(ABC):
    """Abstract base class that every platform source must implement.

    Every subclass sets `code`, `name`, and `domains`, and is listed in `flnd.sources.SOURCES`.
    """

    # The prefix in a target and the cookie file name, such as "ins".
    code: str
    # Shown in help and in prompt mode, such as "Instagram".
    name: str
    # Cookie domains that belong to this source, such as ("instagram.com",).
    domains: Tuple[str, ...]

    @abstractmethod
    def download(
        self,
        target: Target,
        download_dir: Path,
        limit: Optional[int] = None,
        headless: bool = True,
    ) -> int:
        """
        Execute the download for the given target.

        Args:
            target: The parsed Target object (source and value/username).
            download_dir: The root download directory (.flnd/downloads).
            limit: Maximum number of media items/posts to download (None for all).
            headless: Whether to run Playwright in headless mode.

        Returns:
            The total number of newly downloaded files.
        """
        pass
