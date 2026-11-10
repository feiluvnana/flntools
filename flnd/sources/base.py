"""
Base class for all source extractors (tik, fbk, ins, twt).

Each source has full autonomy over:
  - Loading its cookies
  - Choosing its scraping mechanism (HTTPX API, Playwright, or hybrid)
  - Extracting media and platform-specific features (posts, reels, stories, highlights, albums)
  - Organizing files in `.flnd/downloads/<source>/<user>/<subfolder>/`
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from flnd.target import Target


class BaseSource(ABC):
    """Abstract base class that every platform source must implement."""

    # 3-letter source identifier (tik, fbk, ins, twt)
    code: str

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
