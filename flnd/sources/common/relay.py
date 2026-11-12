"""Meta's server-rendered data: the GraphQL results Instagram and Facebook embed in their pages.

A page's first load doesn't fetch its own data. The server puts the results the site's GraphQL
would have returned into `<script type="application/json">` blocks, under `__bbox.result`, so a
source reads them there as well as from the responses that come later.

    for result in embedded_results(session.page.document):
        on_graphql(session, result, None)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from shared._selector import Selection

SCRIPTS = 'script[type="application/json"]::text'


def embedded_results(page: Selection) -> list[Any]:
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
