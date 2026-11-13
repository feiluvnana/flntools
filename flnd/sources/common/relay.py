"""Meta's server-rendered data: the GraphQL results Instagram and Facebook embed in their pages.

A page's first load doesn't fetch its own data. The server puts the results the site's GraphQL
would have returned into `<script type="application/json">` blocks, under `__bbox.result`, so a
source reads them there as well as from the responses that come later. `meta_results` loads a
page and reads them, for a ported source; `embedded_results` reads a page already loaded.

    for result in meta_results(site, f"https://www.instagram.com/{user}/"):
        on_graphql(result)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from flnd.source import Site
    from shared import selector

__all__ = ["embedded_results", "meta_results"]

SCRIPTS = 'script[type="application/json"]::text'


def meta_results(site: Site, url: str) -> list[Any]:
    """Load a Facebook or Instagram page and return the GraphQL results its HTML embeds; [] when it didn't load.

        for result in meta_results(site, f"https://www.instagram.com/p/{code}/"):
            yield from posts_of(result)

    Args:
        site: the target's site; `goto` loads the page, paced, waiting out a 429.
        url: the page.

    Returns:
        Each result, shaped like a response: `{"data": {…}}`, in the page's order.
    """
    if not site.goto(url):
        return []
    return embedded_results(site.page.document)


def embedded_results(page: selector.Selection) -> list[Any]:
    """Every GraphQL result embedded in a page, each shaped like a response: `{"data": {…}}`."""
    found = []
    for text in page.css(SCRIPTS).all:
        if "__bbox" not in text:
            continue
        try:
            found.extend(_results(json.loads(text)))
        except ValueError:
            # A block that isn't JSON holds no results; the page's other blocks still count.
            continue
    return found


def _results(node: Any) -> Iterator[Any]:
    if isinstance(node, dict):
        box = node.get("__bbox")
        if isinstance(box, dict) and "result" in box:
            yield box["result"]
            return
        for value in node.values():
            yield from _results(value)
    elif isinstance(node, list):
        for value in node:
            yield from _results(value)
