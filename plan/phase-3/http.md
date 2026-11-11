# http

Phase 3. Depends on [log](../phase-1/log.md), [fmt](../phase-1/fmt.md), and [path](path.md). Needs the `http` extra (`httpx`).

[`shared/_http.py`](../../shared/_http.py). `http.Client` is a subclass of `httpx.Client`. Every httpx method and attribute works unchanged. The subclass adds defaults that scripts keep writing by hand, plus retries and three methods: `json`, `html`, and `download`.

Today flnd opens its own `httpx.Client` 8 times with a hand-written timeout and headers. It writes the same desktop Chrome user-agent string 6 times, and has a 60-line `download_file`.

```python
with http.Client(user_agent=http.agents.chrome, cookies=cookies, base_url="https://x.com") as client:
    r = client.get("/i/api/...", params={"id": 1})  # a normal httpx.Response
    data = client.json("/i/api/...")                # GET, raise on status, parse JSON
    page = client.html("/example_user")            # GET, raise on status, parsel.Selector
    data = client.json("/api", method="POST", json={"q": 1})
    client.download(url, dest)                      # streamed, atomic, with a progress bar
```

## Client

`http.Client(*, user_agent=agents.chrome, headers=None, cookies=None, timeout=30.0, retries=2, follow_redirects=True, **httpx_kwargs)`

| Argument | Meaning |
| --- | --- |
| `user_agent` | sets the `User-Agent` header. `None` keeps httpx's own |
| `headers` | merged over the user-agent |
| `cookies` | an httpx cookies value, a `dict`, or the `list[dict]` that `flnd.cookies.load_cookies` returns. A list keeps each cookie's domain and path |
| `timeout` | seconds, passed to httpx |
| `retries` | extra attempts after a connection error, a `429`, or a `5xx`; `0` turns retries off |
| anything else | passed to `httpx.Client` |

**Retries.** The waits are 1 s, then 2 s, then 4 s. A `Retry-After` header, in seconds or as an HTTP date, replaces the wait; a longer one waits 60 s. Each retry writes `log.debug("retry {n} {url}: {reason}")`. Only `GET`, `HEAD`, `PUT`, `DELETE`, and `OPTIONS` are retried by default. `client.request(..., retry=True)` opts in a `POST`. Retries are a custom transport wrapped around httpx's own, so `client.stream` gets them too.

`http.agents` holds current user-agent strings, so scripts don't copy them around:

```python
http.agents.chrome   # desktop Chrome on Windows
http.agents.safari   # desktop Safari on macOS
http.agents.iphone   # mobile Safari on iPhone
http.agents.android  # Chrome on Android
```

Strings specific to one app, such as Instagram's Android app UA, stay in the source that needs them.

## json

`client.json(url, *, method="GET", **request_kwargs) -> Any`

1. Sends the request.
2. Calls `raise_for_status()`, so a `4xx` or `5xx` raises `httpx.HTTPStatusError`.
3. Returns `response.json()`. A body that is not JSON raises `http.JSONError`, which carries the status, the content type, and the first 200 characters of the body. Today this is the most common silent failure in flnd's API calls.

## html

`client.html(url, *, method="GET", **request_kwargs) -> parsel.Selector` works like `json`: it sends the request, calls `raise_for_status()`, and returns [`scrape.select(response)`](scrape.md#select). It needs the `scrape` extra. Without it, it raises the extra's `ImportError`, and the rest of `http` still works.

## download

`client.download(url, dest, *, label=None, skip=True, headers=None) -> bool`

1. If `skip` is set and `dest` already exists with a size above 0, it writes `log.info("skip {name}")` and returns `False`.
2. Streams the body into `path.atomic(dest)`, inside `log.progress(label or dest.name, total=content_length, show=fmt.bytes)`. When there is no `Content-Length`, the bar is a spinner.
3. A status other than `200` or `206` raises `httpx.HTTPStatusError` inside the bar, so the bar ends at `error` and no file is left behind.
4. Returns `True`.

It raises instead of returning `False` on failure, so callers decide what a failure means. flnd's wrapper catches the error and counts it.

`http.download(url, dest, **kwargs)` does the same with a short-lived default client, for one-off scripts.

## Tests

All tests use `httpx.MockTransport`, so nothing goes to the network.

- Defaults:
  - the user-agent header;
  - headers merged over it;
  - each cookie input form, and a `list[dict]` keeping domains.
- Retries:
  - on a connection error, `429`, and `503`;
  - the backoff waits, with sleep patched;
  - `Retry-After` honored, and capped at 60 s;
  - no retry on `400`;
  - `POST` retried only with `retry=True`;
  - `retries=0`.
- `json`:
  - the success path;
  - `HTTPStatusError`;
  - `JSONError` and its message.
- `html`: returns a selector, and raises on a `404`.
- `download`:
  - a skip when the file exists;
  - progress with and without `Content-Length`;
  - a non-200 status leaves no file and ends the bar at `error`;
  - the atomic replace of an existing file;
  - the module-level `http.download`.
