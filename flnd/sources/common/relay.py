"""Meta's server-rendered GraphQL results embedded in Instagram and Facebook pages."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from flnd.source import Site
    from flnd import _html

__all__ = ["embedded_results", "meta_results"]

SCRIPTS = 'script[type="application/json"]::text'


def meta_results(site: Site, url: str) -> list[Any]:
    """Load a Facebook or Instagram page and return its embedded GraphQL results, or []."""
    if not site.open(url):
        return []
    return embedded_results(site.document())


def embedded_results(page: _html.Selection) -> list[Any]:
    """Every GraphQL result embedded in a page, each shaped like a response: `{"data": {…}}`."""
    found = []
    for text in page.css(SCRIPTS).get():
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
