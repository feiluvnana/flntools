# source

Phase 6, step 3, with [cli](cli.md) and [session](session.md). The user asked on 2026-11-12 for a leaner `Source`: fewer fields, each one meaning one thing, and merged where two say the same. Today a source can set 18 fields and 7 hooks, 25 in all. Several are never overridden, one is never read, and three pairs only make sense together.

> **Changed by [architecture](architecture.md)** (decided 2026-11-12, flnd adopts gallery-dl's architecture): `run(session)` becomes `items(session)`, a generator of `Post`, `File` and `Queue`, and the source gains `folder`, `name` and `key` format strings with defaults. 11 fields and 5 hooks.


## Today, counted 2026-11-12

| Field or hook | Set by | Read by |
| --- | --- | --- |
| `code`, `name`, `domains`, `examples` | every source | target parsing, help, errors |
| `categories`, `default_categories` | every source | `--from` |
| `short_links` | fbk, tik | `target.resolve`, which follows them with one HEAD |
| `kinds_of_target` | ins, twt, tik, tlg, loi | **nothing** |
| `needs` | fbk, ins, twt, tik, loi | `login.store`, `logged_in` |
| `requires_login`, `login_methods` | tlg | the engine, `flnd login` |
| `user_agent` | tik, twt, with the same Chrome string | the HTTP client |
| `viewport`, `concurrency`, `pause` | **no source** | the engine |
| `browser_args` | tik (`--no-sandbox`) | `browser.connect` |
| `stealth` | twt (`False`) | `browser.connect` |
| `wait` | tlg (Telegram's `FloodWaitError` seconds) | retries |
| `resolve`, `run` | every source | the engine |
| `headers`, `sign` | twt and loi (`headers`), loi (`sign`) | the HTTP client |
| `login`, `logged_in`, `connect` | tlg | the engine, `flnd login` |

## Phase 6: 8 fields and 5 hooks

```python
class Instagram(Source):
    code, name = "ins", "Instagram"
    domains = ("instagram.com", "instagr.am")
    examples = ("ins:example_user", "https://www.instagram.com/p/ABC123/")
    # Each category, and whether a run without --from takes it: the account's own content does.
    categories = {"posts": True, "reels": True, "stories": True, "highlights": True, "profile": True,
                  "tagged": False, "reposts": False}
    cookies = ("sessionid",)
    pace = Pace(floor=1.0, budgets={"/graphql": (275, 600), "/api/v1/": (75, 660)})

    def resolve(self, text): ...
    def run(self, session): ...
    def prepare(self, request, cookies): ...
```

| Field | Means | Was |
| --- | --- | --- |
| `code`, `name` | the target prefix and cookie file name, and the name in help | — |
| `domains` | the site's hosts, short-link hosts included | `domains` and `short_links` |
| `examples` | one example per accepted form, for help and errors | — |
| `categories` | `{name: taken by default}`, in the source's order | `categories` and `default_categories` |
| `cookies` | cookie names, any one of which proves a cookie login | `needs` |
| `logins` | the `flnd login` methods, `("browser", "file")` by default | `login_methods` |
| `pace` | how fast it may send requests to its own domains: a floor in seconds, and known budgets per kind of request; a number is shorthand for the floor ([pace](pace.md)) | new |

| Hook | Means | Was |
| --- | --- | --- |
| `resolve(text) -> Target` | required: read a name, an id or a URL | — |
| `run(session)` | required, may be `async`: download one target | — |
| `prepare(request, cookies) -> request` | add a request's headers and sign it, before every attempt; called only for requests to the source's `domains`, never for a CDN | `headers` and `sign` |
| `login(session, method)` | a login of the source's own (tlg's QR and phone) | — |
| `connect(session)` | the SDK client for `session.client`; raises `LoginError` when there's no login | `connect`, `logged_in` and `requires_login` |

## What goes, and why

- **`kinds_of_target`:** nothing reads it. A target's kind is whatever `resolve` returns.
- **`short_links`:** when `resolve` can't read a URL on one of the source's `domains`, `target.resolve` follows it once with a HEAD request and calls `resolve` again. A short link is just a URL `resolve` doesn't know, so a source no longer lists its short-link hosts separately. They stay in `domains`, as they must today anyway.
- **`default_categories`:** merged into `categories`, so a category and its default can't drift apart. Today a name in `default_categories` that isn't in `categories` is a silent mistake.
- **`requires_login` and `logged_in`:** a cookie source works without a login, using public access, as today. A source with an SDK client can't, and says so where it already finds out: its `connect` raises `LoginError("tlg: not logged in; run flnd login")`, and the engine fails its targets with that message. The engine checks cookie logins itself, from `cookies`.
- **`user_agent`:** every source's HTTP requests send one current desktop Chrome user agent, kept in `flnd/net.py`. tik and twt carry that same string today, and the others send httpx's own, which is the easier one for a site to refuse.
- **`viewport`, `concurrency`, `pause`:** no source sets them. They become the engine's constants: a 1280×900 viewport, 4 downloads at once, and 5 s between two targets.
- **`browser_args`:** tik's `--no-sandbox` turns off Chrome's sandbox, which no site needs and which makes a real Chrome warn on every launch. It goes, with a live tik check.
- **`stealth`:** twt turns it off only because "this browser never hid `navigator.webdriver`", which is history, not a reason. Every source hides it, with a live twt check.
- **`wait`:** tlg's only use is Telegram's flood wait. Its download function catches `FloodWaitError` and calls `session.throttled(retry_after=error.seconds)`, the same path as every other rate limit ([session](session.md#members)). Retry waits are otherwise the engine's 1.0 s.
- **`headers` and `sign`** become one `prepare`, which sees each request and the login's cookies. It runs only for the source's own `domains` ([fixes › Twitter headers](fixes.md#twitter-headers)). loi's `sign` then needs no code to keep its API headers off CDN requests.

The session's [`login_walls`](session.md#members) field isn't added either: ins checks the final URL itself in `run` and calls `session.throttled`. So the `Source` gains no fields in Phase 6.

## Tests

- `tests/test_sources.py`:
  - every source declares only the eight fields;
  - its default categories (the `True` ones) are a non-empty subset;
  - its `examples` all `resolve`.
- `tests/typing/usage.py`: a `Source` with `default_categories`, `needs` or `short_links` doesn't type-check (`ClassVar`s are declared only in `Source`, and a test checks subclasses add no other public class attributes).
- `target.resolve` of a short link that `resolve` can't read follows it once, and a second unreadable URL is a `TargetError` without a second request.
- A tlg target without a login fails with `connect`'s message. A cookie source without one runs with a public-access warning.
- `prepare` runs for `x.com` API requests and not for `pbs.twimg.com` downloads.
- Live, once each: tik without `--no-sandbox`, and twt with stealth on, still load and download ([live targets](README.md#order)).
