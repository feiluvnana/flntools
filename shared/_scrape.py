"""Getting data out of HTML and JSON with parsel and JMESPath.

`select` returns a real `parsel.Selector`. Sources are duck-typed, so this module imports
neither httpx nor Playwright.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any, Iterator, Union

try:
    import jmespath
    import parsel
except ImportError:
    raise ImportError("shared.scrape needs parsel: pip install 'flntools[scrape]'") from None

Selection = Union[parsel.Selector, parsel.SelectorList]
WHITESPACE = re.compile(r"\s+")


def select(source: Any) -> parsel.Selector:
    """A selector over HTML text, bytes, a file, an httpx response, or a Playwright page.

    A response with a JSON content type gives a JSON selector, so `.jmespath()` works.
    """
    if isinstance(source, parsel.Selector):
        return source
    content, kind = _read(source)
    return parsel.Selector(text=content, type=kind)


def text(selection: Selection) -> str:
    """Every text node under the selection, joined, with runs of whitespace collapsed."""
    if isinstance(selection, parsel.Selector):
        selection = parsel.SelectorList([selection])
    pieces = selection.xpath("descendant-or-self::text()").getall()
    return WHITESPACE.sub(" ", " ".join(pieces)).strip()


def script(source: Any, *, id: str | None = None, pattern: str | None = None) -> Any | None:
    """JSON embedded in a page: the text of `<script id=...>`, or the first group of `pattern`.

    Returns None when nothing is found. Text that is found but is not JSON raises
    `json.JSONDecodeError`, because that means the site changed.
    """
    if (id is None) == (pattern is None):
        raise ValueError("pass exactly one of id or pattern")
    if pattern is not None:
        content = source.get() if isinstance(source, parsel.Selector) else _read(source)[0]
        match = re.search(pattern, content, re.DOTALL)
        found = match.group(1) if match else None
    else:
        found = select(source).xpath("//script[@id=$id]/text()", id=id).get()
    return None if found is None else json.loads(found)


def dig(data: Any, *paths: str, default: Any = None) -> Any:
    """The first JMESPath expression that gives something other than None, or `default`.

    A missing key anywhere along a path gives None instead of raising. An empty list counts
    as found, unlike JMESPath's own `a || b`.
    """
    for expression in paths:
        value = _compiled(expression).search(data)
        if value is not None:
            return value
    return default


def find(data: Any, key: str) -> Iterator[Any]:
    """Every value stored under `key`, at any depth, depth-first in document order."""
    if isinstance(data, dict):
        for name, value in data.items():
            if name == key:
                yield value
            yield from find(value, key)
    elif isinstance(data, list):
        for value in data:
            yield from find(value, key)


def _read(source: Any) -> tuple[str, str]:
    """The source's text and whether parsel should read it as "html" or "json"."""
    if isinstance(source, str):
        return source, "html"
    if isinstance(source, (bytes, bytearray)):
        return bytes(source).decode("utf-8", "replace"), "html"
    if isinstance(source, Path):
        return source.read_text(encoding="utf-8", errors="replace"), "html"
    if hasattr(source, "text") and hasattr(source, "headers"):
        content_type = source.headers.get("content-type", "").lower()
        return source.text, "json" if "json" in content_type else "html"
    if callable(getattr(source, "content", None)):
        return source.content(), "html"
    raise TypeError(f"cannot select from {type(source).__name__}: pass HTML text, bytes, a Path, a response, or a page")


@functools.lru_cache(maxsize=512)
def _compiled(expression: str) -> jmespath.parser.ParsedResult:
    return jmespath.compile(expression)
