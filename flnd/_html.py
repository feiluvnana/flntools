"""Getting data out of HTML and JSON: `parse` an HTML page, and `dig` a path in parsed JSON.

A small library over selectolax's lexbor parser (a C HTML parser with its own CSS engine) and
msgspec's JSON decoder. It knows nothing of flnd: any program can use it.

    from flnd import _html

    page = _html.parse(text)
    page.first("h1").text                                      # "Example User"
    [link.attr("href") for link in page.css("a[href*='/video/']")]
    page.script("__NEXT_DATA__")                               # the JSON in <script id=...>
    page.blocks("__bbox")                                      # every JSON block that names it
    _html.dig(data, "data.user.edges[].node")

What it offers:
1. `parse(text)` reads HTML text or bytes into a `Document`. HTML never fails to parse: lexbor
   repairs broken markup the way a browser does.
2. A `Document` (and each `Element` in it) answers CSS queries: `css` gives every match, `first`
   the first one. An element gives its `text` (whitespace collapsed) and its `attr(name)`.
3. A `Document` also reads the JSON a page embeds: `script(id)` decodes one
   `<script id=...>`, and `blocks(contains)` decodes every `<script type="application/json">`.
   `visible` is the text a reader sees: the page without its scripts and styles.
4. `dig(data, path)` walks parsed JSON by a short path; see its docstring for the four forms.

Who calls it: in flnd, `flnd.source` re-exports `dig`; `flnd.site` parses the page a source's
browser loads, `flnd._net.Response.html` a response body, and `flnd.testing` the pages of its
fakes. What it calls: selectolax, msgspec and `flnd.errors`, nothing else.

The main flow of a read:
1. `parse` checks the size, decodes bytes as UTF-8 (a BOM dropped, bad bytes replaced) and
   builds the tree;
2. `css` or `first` runs lexbor's CSS engine over it, and wraps each node as an `Element`;
3. `script` or `blocks` takes a script's text and decodes it with msgspec, raising `Changed`
   when that text isn't JSON;
4. `dig` walks the decoded value one path step at a time.

There is no XPath and no `::text` or `::attr()` pseudo-element: an element's `text` and `attr`
say the same thing in plain Python.

Rules a change must keep:
- selectolax is imported on the first `parse`, and msgspec on the first decode, so importing
  this module (which `flnd.source` does, for `dig`) costs nothing at startup: `flnd --help`
  must not load either.
- A `Document` is never changed once made, so `visible` works on a copy of the tree.
- One document is read from one thread at a time; lexbor's trees aren't thread-safe.

Errors (from `flnd.errors`): text over 64 MiB, or a script that should hold JSON but doesn't, is
`Changed` (the site changed what it sends); a bad CSS selector or a bad `dig` path is a
`SourceError` (the caller's mistake).
"""

from __future__ import annotations

import functools
import re
from typing import TYPE_CHECKING, Any, NamedTuple

from flnd.errors import Changed, SourceError

if TYPE_CHECKING:
    from selectolax.lexbor import LexborHTMLParser, LexborNode

__all__ = ["Document", "Element", "dig", "parse"]

WHITESPACE = re.compile(r"\s+")
# The largest text `parse` reads: 64 MiB. The biggest page flnd reads (a TikTok or Facebook page
# with its embedded JSON) is a few MB, so this is far above any real page and still small enough
# to parse in memory. Counted in bytes for bytes, in characters for text.
MAX_SIZE = 64 * 1024 * 1024
# The tags whose text a reader never sees.
HIDDEN = ["script", "style"]
JSON_BLOCKS = 'script[type="application/json"]'


# ---------------------------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------------------------

def parse(text: str | bytes) -> Document:
    """Parse an HTML page into a `Document`.

    Bytes are read as UTF-8 (a byte-order mark is dropped, and bad bytes become U+FFFD), which is
    what every page flnd reads is sent as.

    Args:
        text: the page's HTML, as text or bytes; "" gives an empty document.

    Returns:
        The parsed page.

    Raises:
        Changed: the text is over 64 MiB.

    Example:
        >>> page = parse("<h1> Example <b>User</b></h1><a href='/video/1'>one</a>")
        >>> page.first("h1").text, [link.attr("href") for link in page.css("a")]
        ('Example User', ['/video/1'])
    """
    _check_size(len(text), "bytes" if isinstance(text, (bytes, bytearray)) else "characters")
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8-sig", "replace")
    return Document(_parser()(text))


class Element:
    """One element of a parsed page: its text, its attributes, and the elements inside it.

    Made by `Document.css` and `first`, not directly.

    Args:
        node: the lexbor node it wraps.
    """

    __slots__ = ("_node",)

    def __init__(self, node: LexborNode):
        self._node = node

    def css(self, query: str) -> list[Element]:
        """Every element inside this one that matches a CSS selector, in page order.

        Args:
            query: a CSS selector, such as `'a[href*="/video/"]'`; a comma joins several.

        Returns:
            The matches; empty when none match.

        Raises:
            SourceError: the selector isn't valid CSS.
        """
        return [Element(node) for node in _select(self._node, query)]

    def first(self, query: str) -> Element | None:
        """The first element inside this one that matches a CSS selector.

        Args:
            query: a CSS selector.

        Returns:
            The first match, or None when nothing matches.

        Raises:
            SourceError: the selector isn't valid CSS.
        """
        found = _select(self._node, query)
        return Element(found[0]) if found else None

    def attr(self, name: str) -> str | None:
        """The value of one attribute: `"/video/1"` for `href` on `<a href="/video/1">`.

        Args:
            name: the attribute's name, such as "href".

        Returns:
            Its value; None when the element hasn't got it, "" when it has it with no value.
        """
        value = self._node.attributes.get(name)
        if value is None and name in self._node.attributes:
            return ""
        return value

    @property
    def text(self) -> str:
        """All the text inside the element, scripts included, with whitespace collapsed."""
        return WHITESPACE.sub(" ", self._node.text(separator=" ")).strip()

    @property
    def tag(self) -> str:
        """The element's tag name, in lowercase: "a", "script"."""
        return self._node.tag or ""

    def __repr__(self) -> str:
        return f"<Element {self.tag}>"


class Document(Element):
    """A parsed page: an `Element` for the whole `<html>`, plus the JSON the page embeds.

    Made by `parse`. It is never changed once made.

    Args:
        tree: the lexbor parser holding the parsed page.
    """

    __slots__ = ("_tree",)

    def __init__(self, tree: LexborHTMLParser):
        super().__init__(tree.root)
        self._tree = tree

    def script(self, id: str) -> Any:
        """The JSON inside `<script id=...>`, decoded.

        Args:
            id: the script's `id`, such as "__NEXT_DATA__".

        Returns:
            The decoded JSON; None when the page has no such script.

        Raises:
            Changed: the script is there but doesn't hold JSON.
        """
        found = _select(self._node, f'script[id="{id}"]')
        return _loads(found[0].text()) if found else None

    def blocks(self, contains: str = "") -> list[Any]:
        """The JSON of every `<script type="application/json">` block, decoded, in page order.

        A block whose text doesn't hold `contains` is skipped before it is decoded, which is
        how a page of many blocks is read fast. A block that isn't valid JSON is skipped too:
        the page's other blocks still count.

        Args:
            contains: text a block must hold to be decoded, such as "__bbox"; "" takes every
                block.

        Returns:
            The decoded blocks.
        """
        decode = _decoder()
        found = []
        for node in _select(self._node, JSON_BLOCKS):
            text = node.text()
            if contains not in text:
                continue
            try:
                found.append(decode(text))
            except ValueError:  # msgspec's DecodeError is a ValueError
                continue
        return found

    @property
    def visible(self) -> str:
        """The text a reader sees: every text outside `<script>` and `<style>`, collapsed.

        It strips a copy of the tree, so the document itself keeps its scripts.
        """
        copy = self._tree.clone()
        copy.strip_tags(HIDDEN)
        return WHITESPACE.sub(" ", copy.root.text(separator=" ")).strip()

    def __repr__(self) -> str:
        return "<Document>"


def _select(node: LexborNode, query: str) -> list[LexborNode]:
    """The nodes under `node` that match `query`, raising `SourceError` for a bad selector."""
    try:
        return node.css(query)
    except Exception as error:  # selectolax raises its own SelectolaxError, or a ValueError
        raise SourceError(f"invalid CSS selector {query!r}: {error}") from None


@functools.cache
def _parser() -> type[LexborHTMLParser]:
    """selectolax's lexbor parser class, imported on the first `parse`.

    Returns:
        The class.

    Raises:
        ImportError: selectolax isn't installed, with the command that installs it.
    """
    try:
        from selectolax.lexbor import LexborHTMLParser
    except ImportError:
        raise ImportError("flnd._html needs selectolax: pip install selectolax") from None
    return LexborHTMLParser


# ---------------------------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------------------------

def _loads(text: str) -> Any:
    """Decode JSON text.

    Args:
        text: the JSON; a byte-order mark before it is dropped.

    Returns:
        The decoded value.

    Raises:
        Changed: the text is over 64 MiB, or isn't JSON.
    """
    _check_size(len(text), "characters")
    try:
        return _decoder()(text.removeprefix("﻿"))
    except ValueError as error:
        raise Changed(f"expected JSON, got {text[:80]!r}: {error}") from None


@functools.cache
def _decoder() -> Any:
    """`msgspec.json.decode`, imported on first use.

    Returns:
        The function, which takes JSON text or bytes and raises a `ValueError` on bad JSON.

    Raises:
        ImportError: msgspec isn't installed, with the command that installs it.
    """
    try:
        import msgspec.json
    except ImportError:
        raise ImportError("flnd._html needs msgspec: pip install msgspec") from None
    return msgspec.json.decode


def _check_size(size: int, unit: str) -> None:
    """Raise `Changed` when `size` is over `MAX_SIZE`."""
    if size > MAX_SIZE:
        raise Changed(f"refusing {size:,} {unit} of text: the limit is {MAX_SIZE:,}")


# ---------------------------------------------------------------------------------------------
# JSON paths
# ---------------------------------------------------------------------------------------------

class _Search(NamedTuple):
    """A `..[field=value]` step: every dict, at any depth, whose `field` is `value`."""

    field: str
    value: str


# A step is a key (str), an index (int), every item of a list (`ALL`), or a `_Search`.
ALL = None
# One step of a path, matched from where the last one ended. A key runs to the next ".", "[",
# "]" or "|"; the search's value runs to its "]".
STEP = re.compile(r"""
      \.\.\[(?P<field>[^\]=]+)=(?P<value>[^\]]*)\]     # ..[field=value]
    | \[(?P<index>-?\d*)\]                              # [] or [3]
    | (?:^|\.)(?P<key>[^.\[\]|]+)                       # key, or .key after a step
""", re.VERBOSE)


def dig(data: Any, path: str) -> Any:
    """Walk parsed JSON by a path, and return what it reaches.

    The path's forms, which can be chained:
    - `a.b` reads a key, and `[0]` an index (`[-1]` is the last item);
    - `[]` reads the rest of the path from every item of a list: missing ones are left out,
      and lists it reaches are flattened, so `edges[].node.media[]` is every media of every node;
    - `..[field=value]` searches at any depth, the top included, for every dict whose `field`
      is the text `value`, then reads the rest of the path from each, as `[]` does;
    - `a || b` gives the first alternative that finds something (not None, "", [] or {}).

    A path with `[]` or `..[]` gives a list, empty when nothing is found.

    Args:
        data: parsed JSON: dicts, lists and values.
        path: the path, such as `"data.user.edge_owner.edges[].node"`.

    Returns:
        What the path reaches: None when a key or index is missing (or a list, as above).

    Raises:
        SourceError: the path isn't one of the forms above, such as `"a[x]"` or `"a..b"`.

    Example:
        >>> page = {"data": {"items": [{"id": 1}, {"id": 2}, {"x": 3}]}, "next": None}
        >>> dig(page, "data.items[].id"), dig(page, "data.items[0].id")
        ([1, 2], 1)
        >>> dig(page, "next || data.items[1].id")
        2
        >>> dig({"a": {"t": "Album", "n": 1}, "b": [{"t": "Album", "n": 2}]}, "..[t=Album].n")
        [1, 2]
    """
    found = None
    for steps in _compile(path):
        found = _walk(data, steps)
        # A path that spreads always gives a list, empty when a key before the spread is missing.
        if found is None and any(_spreads(step) for step in steps):
            found = []
        if found not in (None, "", [], {}):
            return found
    return found


@functools.lru_cache(maxsize=512)
def _compile(path: str) -> tuple[tuple[Any, ...], ...]:
    """A path's alternatives, each as its steps; cached, since sources reuse a few paths.

    Args:
        path: a whole `dig` path, `||` and all.

    Returns:
        One tuple of steps per alternative.

    Raises:
        SourceError: some part of the path isn't a step.
    """
    alternatives = []
    for alternative in path.split("||"):
        text, steps, at = alternative.strip(), [], 0
        while at < len(text):
            step = STEP.match(text, at)
            if step is None or step.end() == at:
                raise SourceError(f"dig: can't read {text[at:]!r} in {path!r}: use a.b, a[0], "
                                  "a[], ..[field=value] or a || b")
            if step["field"] is not None:
                steps.append(_Search(step["field"], step["value"]))
            elif step["index"] is not None:
                steps.append(int(step["index"]) if step["index"] else ALL)
            else:
                steps.append(step["key"])
            at = step.end()
        alternatives.append(tuple(steps))
    return tuple(alternatives)


def _spreads(step: Any) -> bool:
    """Whether a step reads the rest of the path from many values: `[]` or `..[]`."""
    return step is ALL or isinstance(step, _Search)


def _walk(data: Any, steps: tuple[Any, ...]) -> Any:
    """Follow `steps` from `data`; a spreading step maps the rest over each value, and flattens.

    As in JMESPath, `a[]` flattens one level (a list of lists becomes one list), and a spread
    later in the path flattens the lists the items give.

    Args:
        data: the JSON to walk.
        steps: one alternative from `_compile`.

    Returns:
        What the steps reach; None when a key or index is missing, a list after a spread.
    """
    for position, step in enumerate(steps):
        if not _spreads(step):
            data = _step(data, step)
            if data is None:
                return None
            continue
        items = data if step is ALL and isinstance(data, list) else []
        if isinstance(step, _Search):
            items = list(_search(data, step))
        rest = steps[position + 1:]
        found: list[Any] = []
        if any(map(_spreads, rest)):
            # Another spread later: each item gives a list, and the lists are joined.
            for item in items:
                value = _walk(item, rest)
                if value:
                    found.extend(value)
            return found
        if len(rest) == 1 and isinstance(rest[0], str):
            # The commonest path, `edges[].node`, as one comprehension: twice as fast as the loop.
            key = rest[0]
            return [value for item in items
                    if isinstance(item, dict) and (value := item.get(key)) is not None]
        # The common case, `edges[].node.id`: plain steps from each item, in a tight loop.
        for item in items:
            for later in rest:
                # A key, inlined: this loop runs once per item of a feed page.
                if isinstance(item, dict) and isinstance(later, str):
                    item = item.get(later)
                else:
                    item = _step(item, later)
                if item is None:
                    break
            else:
                if isinstance(item, list) and not rest:
                    found.extend(item)  # `a[]` flattens one level
                else:
                    found.append(item)
        return found
    return data


def _step(data: Any, step: str | int) -> Any:
    """One key or index step from `data`.

    Args:
        data: the JSON reached so far.
        step: a key of a dict, or an index of a list (negative counts from the end).

    Returns:
        The value; None when it is missing, or `data` isn't a dict (for a key) or a list.
    """
    if isinstance(step, int):
        if isinstance(data, list) and -len(data) <= step < len(data):
            return data[step]
        return None
    return data.get(step) if isinstance(data, dict) else None


def _search(data: Any, step: _Search) -> Any:
    """Every dict under `data`, `data` included, whose `step.field` is `step.value`.

    Depth first, a parent before its children, and a match's own children are searched too.
    The walk keeps its own stack, so a deeply nested page can't hit Python's recursion limit.

    Args:
        data: parsed JSON.
        step: the field and the text it must equal.

    Yields:
        Each matching dict, in document order.
    """
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get(step.field) == step.value:
                yield node
            stack.extend(reversed(node.values()))
        elif isinstance(node, list):
            stack.extend(reversed(node))
