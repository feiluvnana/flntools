"""Getting data out of HTML and JSON: `selector.parse(source)` gives a `Selection`.

`parse` reads HTML or JSON text, bytes, file Path, response, or dict/list into a
`Selection`. Queries chain: `css`/`xpath` on HTML, `jmespath`/`jsonpath` on JSON.
`script` and `json` extract embedded JSON. `first` and `get()` return matches.

    from flnd import _html as selector

    page = selector.parse(response)
    page.css("a[href*='/video/']::attr(href)").get()
    page.script(id="__NEXT_DATA__").jmespath("props.user").first
    selector.parse({"data": {"id": "9"}}).jmespath("data.id").first

Text over 64 MB or invalid JSON raises `flnd.errors.Changed`; a bad query, `SourceError`.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Any, Iterator, Union

from flnd.errors import Changed, SourceError, disk

try:
    import parsel
except ImportError:
    raise ImportError("flnd._html needs parsel: pip install parsel") from None

__all__ = ["Selection", "parse"]

WHITESPACE = re.compile(r"\s+")
# The largest text `parse` reads: 64 MiB. The biggest page flnd reads (a TikTok or Facebook page
# with its embedded JSON) is a few MB, so this is far above any real page and still small enough
# for lxml and json to parse in memory. Counted in bytes for bytes and files, characters for text.
MAX_SIZE = 64 * 1024 * 1024
Raw = Union[parsel.Selector, parsel.SelectorList[parsel.Selector]]


# Errors: text that should be JSON but doesn't parse, or is too big, is `Changed` (the site
# changed what it sends); a query that is malformed, or used on the wrong kind of document, is a
# `SourceError` (the source's mistake); a file that can't be read is a `DiskError`. A parser
# package that isn't installed is an `ImportError`, as parsel's own is above.


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
        page.css("a::attr(href)").get()                       # ["/video/1", "/video/2"]
        page.script(id="__NEXT_DATA__").jmespath("props.user").first

    `first` and `get()` give text for HTML matches and Python values for JSON matches. A
    `Selection` is falsy when it has no matches, `len` counts them, and iterating gives one
    `Selection` per match. Made by `parse`, not directly. It is never changed once made; read
    an HTML one from one thread at a time, as lxml needs.
    """

    def __init__(self, raw: Raw):
        self._raw = raw
        self._matches = parsel.SelectorList([raw]) if isinstance(raw, parsel.Selector) else raw
        self._source: str | None = None

    def css(self, query: str) -> Selection:
        """The HTML matches of a CSS selector, with parsel's `::text` and `::attr(name)`."""
        try:
            return Selection(self._matches.css(query))
        except Exception as error:
            raise SourceError(f"invalid CSS selector {query!r}: {error}") from None

    def xpath(self, query: str) -> Selection:
        """The HTML matches of an XPath expression."""
        try:
            return Selection(self._matches.xpath(query))
        except Exception as error:
            raise SourceError(f"invalid XPath {query!r}: {error}") from None

    def jmespath(self, query: str) -> Selection:
        """The JSON result of a JMESPath expression: one match per element when it is a list."""
        compiled = _compile_jmespath(query)
        found: list[Any] = []
        for match in self._matches:
            if match.type not in ("json", "text"):
                raise SourceError(f"cannot use jmespath on {match.type!r}")
            value = compiled.search(match.root)
            if isinstance(value, list):
                found.extend(item for item in value if item is not None)
            elif value is not None:
                found.append(value)
        selectors = (parsel.Selector(root=value, type="json") for value in found)
        return Selection(parsel.SelectorList(selectors))

    def jsonpath(self, query: str) -> Selection:
        """The JSON matches of a JSONPath expression (RFC 9535), in document order."""
        compiled = _compile_jsonpath(query)
        found: list[Any] = []
        for match in self._matches:
            if match.type not in ("json", "text"):
                raise SourceError(f"cannot use jsonpath on {match.type!r}")
            for value in compiled.findall(match.root):
                if isinstance(value, list):
                    found.extend(item for item in value if item is not None)
                elif value is not None:
                    found.append(value)
        selectors = (parsel.Selector(root=value, type="json") for value in found)
        return Selection(parsel.SelectorList(selectors))

    def script(self, id: str) -> Selection:
        """The JSON embedded in `<script id=...>`, or an empty `Selection` when absent."""
        found = self._matches.xpath("//script[@id=$id]/text()", id=id).get()
        if found is None:
            return Selection(parsel.SelectorList())
        return Selection(parsel.Selector(root=_loads(found), type="json"))

    def match(self, pattern: str) -> Selection:
        """The JSON from the first capturing group of `pattern` searched in the source text."""
        haystack = self._source if self._source is not None else "".join(self._matches.getall())
        try:
            found = re.search(pattern, haystack, re.DOTALL)
        except re.error as error:
            raise SourceError(f"invalid regex {pattern!r}: {error}") from None
        if found is None:
            return Selection(parsel.SelectorList())
        try:
            group = found.group(1)
        except IndexError:
            raise SourceError(f"pattern {pattern!r} needs a capturing group for JSON") from None
        return Selection(parsel.Selector(root=_loads(group), type="json"))

    @property
    def json(self) -> Selection:
        """Each match's text parsed as JSON, as a JSON `Selection`; empty when there are no matches.

            page.css("script#data::text").json.jmespath("items").get()
            api.jmespath("payload").json.jmespath("user.id").first     # JSON sent inside a string

        The text of an HTML element is all its text nodes, joined as they are; a text or attribute
        match is its own text; a JSON string is parsed. JSON that is already parsed (a number, a
        list, an object) is kept as it is, so `script(...).json` is the same as `script(...)`.
        It raises `Changed` when a text isn't JSON or is over 64 MB.
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

    def get(self) -> list[Any]:
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
        try:
            _check_size(source.stat().st_size, "bytes")
            raw_bytes = source.read_bytes()
        except OSError as error:
            raise disk(error, source) from error
        source = raw_bytes.decode("utf-8-sig", "replace")
    elif isinstance(source, (bytes, bytearray)):
        _check_size(len(source), "bytes")
        source = bytes(source).decode("utf-8-sig", "replace")
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
    clean = text.removeprefix("\ufeff")
    try:
        value = _msgspec_json().decode(clean)
    except Exception:  # not JSON, so it is read as HTML, which never fails to parse
        return parsel.Selector(text=text, type="html"), text
    return parsel.Selector(root=value, type="json"), None


def _loads(text: str) -> Any:
    """Parse JSON text, raising `Changed` if too large or invalid."""
    _check_size(len(text), "characters")
    clean = text.removeprefix("\ufeff")
    try:
        return _msgspec_json().decode(clean)
    except Exception as error:
        raise Changed(f"expected JSON, got {text[:80]!r}: {error}") from None


def _check_size(size: int, unit: str) -> None:
    """Raise `Changed` when `size` is over `MAX_SIZE`."""
    if size > MAX_SIZE:
        raise Changed(f"refusing {size:,} {unit} of text: the limit is {MAX_SIZE:,}")


@functools.cache
def _msgspec_json() -> Any:
    """`msgspec.json`, imported on first use."""
    try:
        import msgspec.json
    except ImportError:
        raise ImportError("flnd._html needs msgspec: pip install msgspec") from None
    return msgspec.json


@functools.lru_cache(maxsize=256)
def _compile_jmespath(query: str) -> Any:
    """Compiled JMESPath expression, cached across calls."""
    try:
        import jmespath
    except ImportError:
        raise ImportError(
            "flnd._html needs jmespath: pip install jmespath"
        ) from None
    try:
        return jmespath.compile(query)
    except Exception as error:
        raise SourceError(f"invalid JMESPath {query!r}: {error}") from None


@functools.lru_cache(maxsize=256)
def _compile_jsonpath(query: str) -> Any:
    """Compiled JSONPath expression, cached across calls."""
    env = _jsonpath()
    try:
        return env.compile(query)
    except Exception as error:
        raise SourceError(f"invalid JSONPath {query!r}: {error}") from None


@functools.lru_cache(maxsize=None)
def _jsonpath() -> Any:
    """python-jsonpath's strict environment, imported on first use."""
    try:
        import jsonpath
    except ImportError:
        raise ImportError(
            "flnd._html needs python-jsonpath: pip install python-jsonpath"
        ) from None
    # The default environment adds keywords such as `missing`, which can't be keys.
    return jsonpath.JSONPathEnvironment(strict=True)
