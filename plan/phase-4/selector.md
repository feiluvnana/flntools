# selector

Phase 4, step 2. `shared/_selector.py` replaces `shared/_scrape.py` and the response half of `http`. It is a small wrapper over parsel, JMESPath and JSONPath, and needs the `selector` extra (`parsel`, `python-jsonpath`).

```python
page = selector.select(response)                     # httpx or Playwright response, HTML, bytes, JSON text, or page.content
page.css("h1").text                                  # "Example User"
page.css("a[href*='/video/']::attr(href)").all      # ["…", "…"]
page.xpath("//meta[@property='og:image']/@content").first
data = page.script("__NEXT_DATA__")                  # the JSON inside a <script>, as a selection
data.jmespath("props.pageProps.items[]").all         # JMESPath: projections, filters
data.jsonpath("$..video_versions").all               # JSONPath: any depth
data.jmespath("data.user.id").first or ""            # a default is just `or`
selector.select(response, "h1::text").first         # with a query: a shortcut for .css(query)
page.raw                                             # the parsel.Selector
```

## select

`select(source, query="") -> Selection`

| Source | Read as |
| --- | --- |
| `str` | HTML text, or JSON when it parses as JSON |
| `bytes` | UTF-8 with bad bytes replaced, then as `str` |
| an httpx response | `.text`. A JSON content type gives a JSON selection |
| a Playwright response | `.text()` (it is a method there; the review caught the old code reading it as an attribute). The content type is read the same way |
| a `Path` | the file's text |

With `query`, it is a shortcut for `.css(query)` on HTML, or for `.jmespath(query)` on JSON. Sources are detected by duck typing, so `selector` imports neither httpx nor Playwright.

## Selection

One `Selection` covers HTML and JSON. Its four queries (`css`, `xpath`, `jmespath`, `jsonpath`) and `script` return selections. The values you read (`first`, `all`, `text`, `raw`) are properties.

| Member | Returns |
| --- | --- |
| `css(query)`, `xpath(query)` | a `Selection` of the matches |
| `jmespath(query)` | a `Selection` of the [JMESPath](https://jmespath.org) result: projections (`edges[].node`), filters, functions |
| `jsonpath(query)` | a `Selection` of the [JSONPath](https://www.rfc-editor.org/rfc/rfc9535) matches. Recursive descent (`$..video_versions`) finds a key at any depth, in a response whose shape you don't know |
| `first` | the first match, or `None`: text for HTML matches, a Python value (`dict`, `list`, `str`, number) for JSON matches |
| `all` | every match, as a `list`, by the same rule |
| `text` | all text nodes inside, joined, with whitespace collapsed and trimmed; `""` when empty |
| `script(id=None, *, pattern=None)` | the JSON embedded in the page, as a selection: by `<script id=…>`, or by a regex whose first group is the JSON. Exactly one of the two must be given. Not found gives an empty selection; found but broken JSON raises `ValueError` |
| `raw` | the `parsel.Selector` |
| `len()`, `bool()`, iteration | over the matches, so `if page.css(".x"):` works |

**JSON results.** A query whose result is a list gives one item per element; any other result is one item. So `data.jmespath("items[]").all` is the list, and `.first` is its first element. A missing path gives an empty selection, so `.first` is `None` and `.all` is `[]`. There is no `default=` argument: `.first or default` says the same thing.

## Tests

- `select`:
  - each source type;
  - a JSON response;
  - a fake Playwright response whose `.text` is a method;
  - a `Path`.
- `css`, `xpath`, `first`, `all` and `text` on nested HTML; `text` and `first` on an empty selection.
- `jmespath`:
  - a simple path, a projection (`edges[].node`) and an index;
  - a list result split into items, a scalar or object result as one item;
  - a missing path gives an empty selection;
  - an invalid expression raises.
- `jsonpath`:
  - `$..key` at several depths, inside lists, in document order;
  - no match;
  - an invalid expression raises.
- `first` and `all` return Python values on JSON matches, and text on HTML matches.
- `script`:
  - by `id` and by `pattern`;
  - not found;
  - broken JSON;
  - both or neither raise `ValueError`.
- The query shortcut on HTML and on JSON.
