"""Warm tabs: start loading the pages a browser source will open next, in background tabs.

    warm_tabs(site, [f"{SITE}/{user}/reels/", f"{SITE}/{user}/tagged/"])

The engine's `Site` keeps up to six tabs per target (`flnd._browser.MAX_TABS`) loading these,
with the responses they get, and `site.open` of one of them switches to its tab at once. A site
without warm tabs, such as the tests' `FakeSite`, ignores the call. Responses themselves are read
with `site.feed`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from flnd.source import Site

__all__ = ["warm_tabs"]


def warm_tabs(site: Site, urls: Iterable[str]) -> None:
    """Start loading `urls`, in order, in background tabs of the target's browser.

    Args:
        site: the target's site.
        urls: the pages the source will open next.
    """
    warm = getattr(site, "_warm_tabs", None)
    if callable(warm):
        warm(urls)
