# browser

Phase 3. Depends on [log](../phase-1/log.md) and [http](http.md) (for `http.agents`). Needs the `browser` extra (Playwright and Chromium).

[`shared/_browser.py`](../../shared/_browser.py). Playwright's sync API, minus the setup. Every function takes or returns a real Playwright `Page`. There are no wrapper types.

All four flnd sources repeat the same 30 to 40 lines:

1. `sync_playwright()`;
2. `chromium.launch` with the same anti-automation flags;
3. `new_context` with a user-agent and viewport;
4. a loop that fixes cookie domains and calls `add_cookies` inside a try/except;
5. the `navigator.webdriver` init script;
6. an `on_response` handler that parses JSON inside a try/except.

`scroll_page_to_end` already lives in [`flnd/utils.py`](../../flnd/utils.py), and TikTok has its own captcha wait loop. This module is those pieces.

```python
with browser.open(headless=False, user_agent=http.agents.chrome, cookies=cookies) as page:
    stop = browser.capture(page, "graphql", lambda response, data: items.extend(parse(data)))
    page.goto("https://www.instagram.com/example_user/")
    browser.scroll(page, count=lambda: len(items), limit=500)
    browser.handoff(page, done=lambda: not page.locator("#captcha").count(),
                    message="Solve the captcha in the browser window")
    stop()
```

## open

`browser.open(*, headless=True, user_agent=http.agents.chrome, cookies=None, viewport=(1280, 900), stealth=True, slow_mo=0, args=None, **context_kwargs)` is a context manager that yields a `Page`.

- Starts `sync_playwright` and launches Chromium with `--disable-blink-features=AutomationControlled` and `--disable-infobars`. `args` adds more Chromium flags after these, such as TikTok's `--no-sandbox`.
- Creates one context with `user_agent`, `viewport`, and any `context_kwargs` (locale, timezone, proxy, …). Opens one page.
- `cookies` takes the `list[dict]` from `load_cookies`. Each cookie gets a leading `.` on its domain and a default `path` of `/`, which is exactly the fix-up every source does today. Cookies are added one at a time, so if Playwright rejects one, that error is logged with `log.warn` and the others and the page still load.
- `stealth=True` adds the init script that hides `navigator.webdriver`. That is all; deeper stealth is [out of scope](../README.md#out-of-scope).
- `slow_mo` is in seconds, like every time in `shared`, and is converted to Playwright's milliseconds.
- On exit, the context, the browser, and Playwright are closed in that order, even when the block raised.
- `page.context` and `page.context.browser` give the real objects for anything else, such as a second page.

If Chromium is not installed, it raises `RuntimeError("Chromium is not installed: run 'playwright install chromium'")` instead of Playwright's long message.

## capture

`browser.capture(page, match, handler, *, lines=False) -> Callable[[], None]` calls `handler(response, data)` for every response that matches.

- `match` is a substring of the URL, a compiled regex, or a function from response to bool.
- When the body is not JSON, the response is skipped and `log.debug` records the URL.
- `lines=True` parses the body as newline-delimited JSON (Facebook's format) and calls the handler once per line.
- An exception raised inside `handler` is logged with `log.warn`, including the URL, and does not stop later responses. Today flnd's handlers wrap everything in `except Exception: pass`, which hides parser bugs.
- It returns a function that removes the listener.

## scroll

`browser.scroll(page, *, count=None, limit=None, idle=4, delay=2.0, max=200, more=None, step=None, onstep=None) -> int`

This is `flnd.utils.scroll_page_to_end` moved here. It has the same stopping rules. The parameters are renamed to the [naming convention](../README.md#naming), and times are in seconds instead of milliseconds.

It stops at the first of these:
- `count()` reaches `limit`;
- `more()` returns `False`;
- `idle` scrolls in a row change neither the page height nor `count()`;
- `max` scrolls are reached.

`step(page)` replaces the default scroll, which is `window.scrollTo(0, document.body.scrollHeight)`. TikTok needs this. `onstep()` is called after each scroll, so `log.progress` can advance. It returns the number of scrolls.

## handoff

`browser.handoff(page, *, done, message, poll=1.0, timeout=None) -> bool` is for steps only a person can do, such as a captcha or a login.

1. If `done()` is already true, it returns `True` at once.
2. Otherwise it writes `log.warn(message)` and polls `done()` every `poll` seconds inside a `log.progress(message)` spinner. It waits with `page.wait_for_timeout`, not `time.sleep`, so Playwright keeps delivering events, such as `capture` responses, while it waits.
3. When `done()` becomes true, it writes `log.ok` and returns `True`. When `timeout` seconds pass first, it returns `False`.

When the browser is headless, a person cannot see the page, so it raises `RuntimeError` instead of waiting forever. Playwright does not report whether a page is headless, so this applies to pages opened by `browser.open(headless=True)`.

## Tests

Tests load a local HTML file through a `file://` URL. They are skipped when Chromium is not installed. `capture` tests serve their responses with `page.route` on an `https://` URL, because a `file://` page cannot `fetch`. `handoff` tests use a fake page with a fake clock, except the headless one.

- `open`:
  - the user-agent and viewport;
  - the cookie domain fix-up;
  - a bad cookie logs a warning and the page still opens;
  - `navigator.webdriver` is undefined;
  - everything is closed after an exception.
- `capture`:
  - each `match` form;
  - a body that is not JSON is skipped;
  - `lines=True`;
  - a handler exception is logged and later responses still arrive;
  - `stop()` removes the listener.
- `scroll`, on a page that adds items while scrolling:
  - each stopping rule;
  - a custom `step`;
  - `onstep` calls.
- `handoff`:
  - already done;
  - done after polling;
  - a timeout;
  - headless raises.
