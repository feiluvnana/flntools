# scrape

Phase 3. Depends on nothing in `shared`. Needs the `scrape` extra (`parsel`, which brings `jmespath`).

[`shared/_scrape.py`](../../shared/_scrape.py). Getting data out of HTML and JSON. It builds on parsel, the selector library inside Scrapy, and returns real `parsel.Selector` objects. It does not wrap Scrapy itself. Scrapy is a crawling framework with its own event loop, spiders, pipelines, and settings, so it cannot run inside a normal function call or next to sync Playwright. The parts of Scrapy that scripts want are covered elsewhere: selectors here, and polite retries and rate limits in [worker](worker.md) and [http](http.md).

flnd already depends on parsel but never imports it. Instead it writes 31 chains like `data.get("data", {}).get("user", {}).get("edge_owner_to_timeline_media", {})` and pulls `__NEXT_DATA__` out of TikTok's HTML with a regex.

```python
page = scrape.select(response)                    # httpx.Response, Page, str, bytes, or Path
page.css("a[href*='/video/']::attr(href)").getall()
scrape.text(page.css("h1"))                       # "Example User": whitespace collapsed

data = scrape.script(response, id="__NEXT_DATA__")                     # JSON inside a <script> tag
data = scrape.script(response, pattern=r"window\._sharedData = (\{.*?\});")

edges = scrape.dig(data, "data.user.edge_owner_to_timeline_media.edges[].node",
                         "data.xdt_api__v1__feed__user_timeline_graphql_connection.edges[].node",
                         default=[])
urls = list(scrape.find(data, "video_versions"))  # every value under that key, at any depth
```

## select

`scrape.select(source) -> parsel.Selector` accepts:

| Source | Read as |
| --- | --- |
| `str` | HTML text |
| `bytes` | decoded as UTF-8, with bad bytes replaced |
| `Path` | the file's text |
| an `httpx.Response` (anything with `.text` and `.headers`) | its text; a JSON content type gives a JSON selector, so `.jmespath()` works |
| a Playwright `Page` (anything with `.content()`) | `page.content()`, which is the live DOM after scripts ran |

It uses duck typing, so `scrape` imports neither httpx nor Playwright. The result is a plain `parsel.Selector`: `.css()`, `.xpath()`, `.re()`, `.jmespath()`, `.get()`, and `.getall()` all work as the parsel docs describe.

## text

`scrape.text(selection) -> str` takes a `Selector` or a `SelectorList`. It joins every text node under it, collapses runs of whitespace to one space, and strips both ends. An empty selection gives `""`. This replaces the common `" ".join(sel.css("::text").getall()).split()` dance.

## script

`scrape.script(source, *, id=None, pattern=None) -> Any | None` returns JSON embedded in a page.

- `id="__NEXT_DATA__"` reads the text of `<script id="__NEXT_DATA__">`.
- `pattern=r"…(\{.*?\})…"` searches the page's text with a regex. Its first group is the JSON.
- Exactly one of `id` and `pattern` must be given, otherwise it raises `ValueError`.
- It returns `None` when the tag or pattern is not found. It raises `json.JSONDecodeError` (a `ValueError`) when the text is found but is not valid JSON, because that means the site changed.

`source` is anything `select` accepts.

## dig

`scrape.dig(data, *paths, default=None) -> Any` reads a value from nested JSON with [JMESPath](https://jmespath.org) expressions, such as `data.user.edges[0].node.id` or `items[].video.url`.

- Each path is tried in order. The first that gives a result other than `None` wins, and `default` is returned when none do. This is how a script handles an API that has two or three response shapes. (JMESPath's own `a || b` treats an empty list as missing, so separate paths are clearer.)
- A missing key anywhere along a path gives `None`, never `KeyError` or `TypeError`. That is the whole point of `.get("x", {})` chains, which `dig` replaces.
- Compiled expressions are cached, so `dig` inside a loop over thousands of items stays cheap.
- An invalid expression raises `jmespath.exceptions.ParseError` the first time it is used.

## find

`scrape.find(data, key) -> Iterator[Any]` yields every value stored under `key`, at any depth, depth-first, in document order. It is for JSON whose shape changes but whose leaf names stay stable: Instagram's `video_versions`, `image_versions2`, and `edges`. It walks dicts and lists only.

## Used by http

[http](http.md) gets one method that matches `client.json`:

```python
page = client.html(url)      # GET, raise on status, then scrape.select(response)
```

## Tests

- `select`:
  - each source type;
  - a JSON response gives a selector that `.jmespath()` works on;
  - a fake `Page` with `.content()`.
- `text`:
  - nested tags;
  - whitespace and newlines;
  - an empty selection.
- `script`:
  - by `id`;
  - by `pattern`;
  - not found returns `None`;
  - broken JSON raises;
  - neither or both arguments raise `ValueError`.
- `dig`:
  - a simple path, a projection (`edges[].node`), and a list index;
  - the first of several paths, and a fallback to the second;
  - an empty list counts as found;
  - `default`;
  - a missing key mid-path;
  - the expression cache;
  - a bad expression raises.
- `find`: depth-first order, keys inside lists, and no match yields nothing.
- Each Instagram `.get` chain in `flnd/sources/ins.py`, rewritten with `dig`, gives the same result on the JSON fixtures in [`tests/test_sources.py`](../../tests/test_sources.py).
