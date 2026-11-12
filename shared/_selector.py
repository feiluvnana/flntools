"""Getting data out of HTML and JSON: CSS, XPath, JMESPath and JSONPath over one `Selection`.

A small wrapper over parsel. Sources are detected by duck typing, so this module imports
neither httpx nor Playwright.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any, Iterator, Union

import shared

try:
    import parsel
except ImportError:
    raise ImportError("shared.selector needs parsel: pip install 'flntools[selector]'") from None

__all__ = ["Error", "Selection", "select"]

WHITESPACE = re.compile(r"\s+")
Raw = Union[parsel.Selector, parsel.SelectorList[parsel.Selector]]


class Error(shared.Error):
    """The base of every error `shared.selector` raises."""


def select(source: Any, query: str = "") -> Selection:
    """A selection over HTML or JSON, read from text, bytes, a file, a response, or a parsed value.

        page = select(response)
        select(response, "h1::text").first      # same as select(response).css("h1::text").first

    A `str` that parses as JSON, a `dict` or `list`, and a response with a JSON content type
    give a JSON selection. With `query`, this is `.css(query)` on HTML or `.jmespath(query)`
    on JSON. A response with a JSON content type but broken JSON raises `ValueError`.
    """
    root = _selector(source)
    selection = Selection(root)
    if not query:
        return selection
    return selection.jmespath(query) if root.type == "json" else selection.css(query)


class Selection:
    """The matches of a query over HTML or JSON, with each query returning a new selection.

        page = select(html)
        page.css("a::attr(href)").all            # ["/video/1", "/video/2"]
        page.script("__NEXT_DATA__").jmespath("props.user").first

    `first` and `all` give text for HTML matches and Python values for JSON matches.
    """

    def __init__(self, raw: Raw):
        self._raw = raw
        self._matches = parsel.SelectorList([raw]) if isinstance(raw, parsel.Selector) else raw

    def css(self, query: str) -> Selection:
        """The HTML matches of a CSS selector, with parsel's `::text` and `::attr(name)`.

            page.css("a[href*='/video/']::attr(href)").all
        """
        return Selection(self._matches.css(query))

    def xpath(self, query: str) -> Selection:
        """The HTML matches of an XPath expression.

            page.xpath("//meta[@property='og:image']/@content").first
        """
        return Selection(self._matches.xpath(query))

    def jmespath(self, query: str) -> Selection:
        """The JSON result of a JMESPath expression: one match per element when it is a list.

            data.jmespath("data.user.id").first or ""
            data.jmespath("edges[].node").all

        A missing path gives an empty selection. An invalid expression raises
        `jmespath.exceptions.ParseError`.
        """
        return Selection(self._matches.jmespath(query))

    def jsonpath(self, query: str) -> Selection:
        """The JSON matches of a JSONPath expression (RFC 9535), in document order.

            data.jsonpath("$..video_versions").all    # the key at any depth

        An invalid expression raises `jsonpath.JSONPathSyntaxError`. On HTML it raises
        `ValueError`, as `css` does on JSON.
        """
        found = []
        for match in self._matches:
            if match.type not in ("json", "text"):
                raise ValueError(f"Cannot use jsonpath on a Selector of type {match.type!r}")
            found.extend(_jsonpath().findall(query, match.root))
        return Selection(parsel.SelectorList(parsel.Selector(root=value, type="json") for value in found))

    def script(self, id: str | None = None, *, pattern: str | None = None) -> Selection:
        """The JSON embedded in the page, from `<script id=...>` or the first group of `pattern`.

            page.script("__NEXT_DATA__").jmespath("props.pageProps").first
            page.script(pattern=r"window\\._sharedData = (\\{.*?\\});")

        Pass exactly one of `id` and `pattern`, or it raises `ValueError`. Nothing found gives an
        empty selection. Text found but not JSON raises `ValueError`, because the site changed.
        """
        if (id is None) == (pattern is None):
            raise ValueError("pass exactly one of id or pattern")
        if id is not None:
            found = self._matches.xpath("//script[@id=$id]/text()", id=id).get()
        else:
            match = re.search(pattern or "", "".join(self._matches.getall()), re.DOTALL)
            found = match.group(1) if match else None
        if found is None:
            return Selection(parsel.SelectorList())
        return Selection(parsel.Selector(root=json.loads(found), type="json"))

    @property
    def first(self) -> Any:
        """The first match, or None: text for HTML, a Python value for JSON."""
        return self._matches.get()

    @property
    def all(self) -> list[Any]:
        """Every match, as a list: text for HTML, Python values for JSON."""
        return self._matches.getall()

    @property
    def text(self) -> str:
        """Every text node inside the HTML matches, joined, with whitespace collapsed; "" when empty."""
        pieces = self._matches.xpath("descendant-or-self::text()").getall()
        return WHITESPACE.sub(" ", " ".join(pieces)).strip()

    @property
    def raw(self) -> Raw:
        """The parsel object: a `parsel.Selector` from `select` and `script`, else a `parsel.SelectorList`."""
        return self._raw

    def __len__(self) -> int:
        return len(self._matches)

    def __bool__(self) -> bool:
        return len(self._matches) > 0

    def __iter__(self) -> Iterator[Selection]:
        return (Selection(match) for match in self._matches)

    def __repr__(self) -> str:
        return f"Selection({self._matches!r})"


def _selector(source: Any) -> parsel.Selector:
    """The parsel selector for a source, as described in `select`."""
    if isinstance(source, (dict, list)):
        return parsel.Selector(root=source, type="json")
    if isinstance(source, Path):
        source = source.read_text(encoding="utf-8", errors="replace")
    elif isinstance(source, (bytes, bytearray)):
        source = bytes(source).decode("utf-8", "replace")
    elif hasattr(source, "text") and hasattr(source, "headers"):
        return _response_selector(source)
    if isinstance(source, str):
        return _text_selector(source)
    raise TypeError(f"cannot select from {type(source).__name__}: pass text, bytes, a Path, a response, "
                    "or a dict or list")


def _response_selector(response: Any) -> parsel.Selector:
    # Playwright's `text` is a method and httpx's is an attribute; both have `headers`.
    text = response.text if isinstance(response.text, str) else response.text()
    if "json" in response.headers.get("content-type", "").lower():
        return parsel.Selector(root=json.loads(text), type="json")
    return _text_selector(text)


def _text_selector(text: str) -> parsel.Selector:
    try:
        value = json.loads(text)
    except ValueError:
        return parsel.Selector(text=text, type="html")
    return parsel.Selector(root=value, type="json")


@functools.lru_cache(maxsize=None)
def _jsonpath() -> Any:
    try:
        import jsonpath
    except ImportError:
        raise ImportError("shared.selector needs python-jsonpath: pip install 'flntools[selector]'") from None
    # The default environment adds keywords such as `missing`, which then can't be used as key names.
    return jsonpath.JSONPathEnvironment(strict=True)
