"""Getting data out of HTML and JSON: `selector.parse(source)` gives a `Selection`.

`parse` reads HTML or JSON text, bytes, file Path, response, or dict/list into a
`Selection`. Queries chain: `css`/`xpath` on HTML, `jmespath`/`jsonpath` on JSON.
`script` and `json` extract embedded JSON. `first` and `all` return matches.

    from shared import selector

    page = selector.parse(response)
    page.css("a[href*='/video/']::attr(href)").all
    page.script(id="__NEXT_DATA__").jmespath("props.user").first
    selector.parse({"data": {"id": "9"}}).jmespath("data.id").first

Text over 64 MB raises `UnsafeError`. Invalid JSON raises `BrokenError`.
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

__all__ = ["BrokenError", "Error", "Selection", "UnsafeError", "parse"]

WHITESPACE = re.compile(r"\s+")
# The largest text `parse` reads: 64 MiB. The biggest page flnd reads (a TikTok or Facebook page
# with its embedded JSON) is a few MB, so this is far above any real page and still small enough
# for lxml and json to parse in memory. Counted in bytes for bytes and files, characters for text.
MAX_SIZE = 64 * 1024 * 1024
Raw = Union[parsel.Selector, parsel.SelectorList[parsel.Selector]]


class Error(shared.Error):
    """The base of every error `shared.selector` raises."""


class UnsafeError(Error, shared.UnsafeError):
    """Text over 64 MB, refused before it is parsed."""


# A ValueError through shared.BrokenError so callers catching ValueError still handle it.
class BrokenError(Error, shared.BrokenError):
    """Text that should be JSON but doesn't parse: the site changed what it sends."""


def parse(source: Any) -> Selection:
    """A `Selection` over HTML or JSON, read from text, bytes, a file, or response.

        page = selector.parse(response)
        page.css("h1::text").first
        selector.parse('{"a": [1, 2]}').jmespath("a[1]").first

    Args:
        source: HTML/JSON text, bytes, Path, response, or parsed dict/list.

    Returns:
        A Selection for querying HTML (css, xpath) or JSON (jmespath, jsonpath).
    """
    raw, text = _selector(source)
    made = Selection(raw)
    made._source = text
    return made


class Selection:
    """The matches of a query over HTML or JSON, with each query returning a new `Selection`.

        page = selector.parse(html)
        page.css("a::attr(href)").all                       # ["/video/1", "/video/2"]
        page.script(id="__NEXT_DATA__").jmespath("props.user").first

    `first` and `all` give text for HTML matches and Python values for JSON matches. A
    `Selection` is falsy when it has no matches, `len` counts them, and iterating gives one
    `Selection` per match. Made by `parse`, not directly. It is never changed once made; read
    an HTML one from one thread at a time, as lxml needs.
    """

    def __init__(self, raw: Raw):
        self._raw = raw
        self._matches = parsel.SelectorList([raw]) if isinstance(raw, parsel.Selector) else raw
        self._source: str | None = None

    def css(self, query: str) -> Selection:
        """The HTML matches of a CSS selector, with parsel's `::text` and `::attr(name)`.

            page.css("a[href*='/video/']::attr(href)").all

        Args:
            query: the CSS selector.

        Raises:
            ValueError: the selection is JSON, not HTML (parsel's error).
            cssselect.SelectorSyntaxError: `query` isn't valid CSS (a SyntaxError, as cssselect
                defines it).
        """
        return Selection(self._matches.css(query))

    def xpath(self, query: str) -> Selection:
        """The HTML matches of an XPath expression.

            page.xpath("//meta[@property='og:image']/@content").first

        Args:
            query: the XPath expression.

        Raises:
            ValueError: selection is JSON not HTML, or invalid XPath (parsel error).
        """
        return Selection(self._matches.xpath(query))

    def jmespath(self, query: str) -> Selection:
        """The JSON result of a JMESPath expression: one match per element when it is a list.

            data.jmespath("data.user.id").first or ""
            data.jmespath("edges[].node").all

        Args:
            query: the JMESPath expression; a missing path gives an empty selection.

        Raises:
            jmespath.exceptions.ParseError: `query` isn't valid JMESPath.
        """
        return Selection(self._matches.jmespath(query))

    def jsonpath(self, query: str) -> Selection:
        """The JSON matches of a JSONPath expression (RFC 9535), in document order.

            data.jsonpath("$..video_versions").all    # the key at any depth

        Args:
            query: the JSONPath expression, in python-jsonpath's strict RFC 9535 syntax.

        Raises:
            jsonpath.JSONPathSyntaxError: `query` isn't valid JSONPath.
            ValueError: the selection is HTML, as `css` raises on JSON.
        """
        found = []
        for match in self._matches:
            if match.type not in ("json", "text"):
                raise ValueError(f"Cannot use jsonpath on a Selector of type {match.type!r}")
            found.extend(_jsonpath().findall(query, match.root))
        selectors = (parsel.Selector(root=value, type="json") for value in found)
        return Selection(parsel.SelectorList(selectors))

    def script(self, id: str | None = None, *, pattern: str | None = None) -> Selection:
        """The JSON embedded in the page, from `<script id=...>` or the first group of `pattern`.

            page.script(id="__NEXT_DATA__").jmespath("props.pageProps").first
            page.script(pattern=r"window\\._sharedData = (\\{.*?\\});")

        Args:
            id: the `id` of the `<script>` element whose text is the JSON.
            pattern: a regular expression searched for in the HTML (with `.` matching newlines),
                whose first group is the JSON.

        Returns:
            A JSON `Selection` over what was found, or an empty one when nothing was.

        Raises:
            ValueError: neither or both of `id` and `pattern` were passed.
            BrokenError: the text was found but isn't JSON, because the site changed.
        """
        if (id is None) == (pattern is None):
            raise ValueError("pass exactly one of id or pattern")
        if id is not None:
            found = self._matches.xpath("//script[@id=$id]/text()", id=id).get()
        else:
            haystack = self._source if self._source is not None else "".join(self._matches.getall())
            match = re.search(pattern or "", haystack, re.DOTALL)
            found = match.group(1) if match else None
        if found is None:
            return Selection(parsel.SelectorList())
        return Selection(parsel.Selector(root=_loads(found), type="json"))

    @property
    def json(self) -> Selection:
        """Each match's text parsed as JSON, as a JSON `Selection`; empty when there are no matches.

            page.css("script#data::text").json.jmespath("items").all
            api.jmespath("payload").json.jmespath("user.id").first     # JSON sent inside a string

        The text of an HTML element is all its text nodes, joined as they are; a text or attribute
        match is its own text; a JSON string is parsed. JSON that is already parsed (a number, a
        list, an object) is kept as it is, so `script(...).json` is the same as `script(...)`.
        It raises `BrokenError` when a text isn't JSON, and `UnsafeError` when it is over 64 MB.
        """
        values = []
        for match in self._matches:
            if match.type == "json":
                values.append(_loads(match.root) if isinstance(match.root, str) else match.root)
            elif isinstance(match.root, str):  # a text node or attribute value, such as `::text`
                values.append(_loads(match.root))
            else:
                values.append(_loads("".join(match.xpath("descendant-or-self::text()").getall())))
        selectors = (parsel.Selector(root=value, type="json") for value in values)
        return Selection(parsel.SelectorList(selectors))

    @property
    def first(self) -> Any:
        """The first match, or None when there is none: text for HTML, a Python value for JSON."""
        return self._matches.get()

    @property
    def all(self) -> list[Any]:
        """Every match, as a list: text for HTML, Python values for JSON."""
        return self._matches.getall()

    @property
    def text(self) -> str:
        """Every text node inside HTML matches, joined and collapsed; "" when empty."""
        pieces = self._matches.xpath("descendant-or-self::text()").getall()
        return WHITESPACE.sub(" ", " ".join(pieces)).strip()

    @property
    def raw(self) -> Raw:
        """The underlying parsel.Selector or parsel.SelectorList."""
        return self._raw

    def __len__(self) -> int:
        return len(self._matches)

    def __bool__(self) -> bool:
        return len(self._matches) > 0

    def __iter__(self) -> Iterator[Selection]:
        return (Selection(match) for match in self._matches)

    def __repr__(self) -> str:
        return f"Selection({self._matches!r})"


def _selector(source: Any) -> tuple[parsel.Selector, str | None]:
    """The parsel selector for a source, plus raw HTML text when available."""
    if isinstance(source, (dict, list)):
        return parsel.Selector(root=source, type="json"), None
    if isinstance(source, Path):
        # Checked before reading, so a huge file is never loaded at all.
        _check_size(source.stat().st_size, "bytes")
        source = source.read_text(encoding="utf-8", errors="replace")
    elif isinstance(source, (bytes, bytearray)):
        _check_size(len(source), "bytes")
        source = bytes(source).decode("utf-8", "replace")
    elif hasattr(source, "text") and hasattr(source, "headers"):
        return _response_selector(source)
    if isinstance(source, str):
        return _text_selector(source)
    raise TypeError(f"cannot parse {type(source).__name__}: pass text, bytes, a Path, a response, "
                    "or a dict or list")


def _response_selector(response: Any) -> tuple[parsel.Selector, str | None]:
    """The selector for an httpx or Playwright response: JSON when its content type says so."""
    # Playwright's `text` is a method and httpx's is an attribute; both have `headers`.
    text = response.text if isinstance(response.text, str) else response.text()
    if "json" in response.headers.get("content-type", "").lower():
        return parsel.Selector(root=_loads(text), type="json"), None
    return _text_selector(text)


def _text_selector(text: str) -> tuple[parsel.Selector, str | None]:
    """A JSON selector when `text` parses as JSON, else an HTML one with its source text."""
    _check_size(len(text), "characters")
    try:
        value = json.loads(text)
    except ValueError:  # not JSON, so it is read as HTML, which never fails to parse
        return parsel.Selector(text=text, type="html"), text
    return parsel.Selector(root=value, type="json"), None


def _loads(text: str) -> Any:
    """Parse JSON text, raising UnsafeError if too large and BrokenError if invalid."""
    _check_size(len(text), "characters")
    try:
        return json.loads(text)
    except ValueError as error:
        raise BrokenError(f"expected JSON, got {text[:80]!r}: {error}") from None


def _check_size(size: int, unit: str) -> None:
    """Raise `UnsafeError` when `size` is over `MAX_SIZE`."""
    if size > MAX_SIZE:
        raise UnsafeError(f"refusing {size:,} {unit} of text: the limit is {MAX_SIZE:,}")


@functools.lru_cache(maxsize=None)
def _jsonpath() -> Any:
    """python-jsonpath's strict environment, imported on first use."""
    try:
        import jsonpath
    except ImportError:
        raise ImportError(
            "shared.selector needs python-jsonpath: pip install 'flntools[selector]'"
        ) from None
    # The default environment adds keywords such as `missing`, which can't be keys.
    return jsonpath.JSONPathEnvironment(strict=True)
