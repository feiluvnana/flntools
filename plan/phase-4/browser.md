# browser

Phase 4, step 2. `shared/_browser.py` is rewritten as two small wrappers over Playwright, `Browser` and `Page`, A connected browser's cookies are `b.cookies`. Cookie files and installed browsers are the [cookies](cookies.md) module. Needs the `browser` extra (`playwright`).

You connect to a browser first. Then you open pages in it:

```python
with browser.connect(headless=False) as b:                     # incognito by default
    b.cookies.add(cookies.load_from_browser("instagram.com"))  # log in with your installed Chrome's cookies
    page = b.open_page("https://www.instagram.com/example_user/")
    stop = page.on_response("graphql", handler)                 # handler(data, response): parsed JSON
    page.scroll(count=lambda: len(items), limit=500)
    page.wait_for_selector("article"); page.wait_for_url("**/reels/"); page.wait_for_load(idle=True)
    page.handoff(until=lambda: not page.selector.css("#captcha"), message="Solve the captcha")
    page.selector.css("h1").text                               # the live page as a selector.Selection
    page.url; page.title; page.raw                              # page getters; raw is the Playwright Page
    b.cookies.all; b.pages; b.raw                               # raw is the BrowserContext
```

## connect

`connect(*, profile=None, cdp=None, headless=True, viewport=(1280, 900), locale=None, timezone=None, args=(), stealth=True) -> Browser`, used as a context manager. `locale` (`"fr-FR"`) and `timezone` (`"Europe/Paris"`) are passed to Playwright's context (`locale`, `timezone_id`), incognito or persistent; `None` keeps the system's. They were added in step 3, with their caller, the engine.

| Choice | What you get |
| --- | --- |
| `profile=None` (the default) | **incognito**: a fresh browser with no history, cookies or cache, all thrown away on close |
| `profile=<folder>` | a persistent profile. Logins, cookies and storage stay in that folder between runs |
| `profile=browser.installed_profile("chrome", "Default")` | a **copy** of your installed Chrome's profile in a temp folder, opened with real Chrome (`channel="chrome"`). The copy avoids Chrome's profile lock, so Chrome can stay open. Changes aren't written back, and the copy is deleted on close |
| `cdp="http://127.0.0.1:9222"` or `cdp="wss://…"` | **attach** to a browser that is already running, through any Chrome DevTools Protocol endpoint. It can be a local Chrome started with `--remote-debugging-port=9222`, or a **cloud browser** service such as Browserless or Browserbase, whose `wss://` URL usually carries a token. It uses that browser's existing context and cookies. On exit, flnd disconnects and leaves the browser running. A local Chrome only allows this when it was started with its own `--user-data-dir`, not your default profile; the docstring says so |

- **Launch:** your installed Chrome (`channel="chrome"`) when there is one, because Playwright's own build differs in codecs and fonts, which fingerprinting sees. Otherwise Playwright's Chromium (`channel="chromium"`), which runs Chrome's new headless mode rather than the old, easily spotted one. It starts with the anti-automation flags, and `args=` adds more (TikTok needs `--no-sandbox`).
- **User agent:** the browser's own, never a made-up one: a Windows string from a Mac contradicts `navigator.platform` and the client-hint headers. The one change is in headless mode, where "HeadlessChrome" is reported as "Chrome". There is no `user_agent=` option. A source that needs a particular user agent sets it on its own HTTP requests.
- **With `cdp=`**, the browser and its context already exist, so `profile`, `headless`, `locale`, `timezone`, `args` and `stealth` don't apply; passing them raises `ValueError`. `viewport` still applies, set on each page flnd opens. `b.cookies.add(jar)` works the same, so a cloud browser can be logged in with cookies from `cookies.load_from_browser`.
- **`stealth=True`** hides `navigator.webdriver`, and nothing more.
- **On exit**, pages, the context, the browser and Playwright close in order, even when the block raised. A failure while closing is logged once and never hides the original error.
- **Missing browser:** a missing Chromium raises `RuntimeError("Chromium is not installed: run 'playwright install chromium'")`. flnd's session replaces it with `Chromium is not installed; run flnd install-browser` ([packaging](packaging.md)).

## Browser

| Member | Does |
| --- | --- |
| `open_page(url=None, *, idle=False, timeout=30.0) -> Page` | opens a new tab, and goes to `url` when given. A navigation error is logged and leaves the page open on `about:blank` |
| `cookies` | the live browser's cookie jar: the `all` property (a `list[dict]` snapshot), `add(jar)`, and `clear_all()`. `add` takes a jar in the [cookies](cookies.md) shape, one cookie at a time, so one rejected cookie (logged with `log.warn`) doesn't drop the rest. Cookies are passed to Playwright **unchanged**, except that `path` defaults to `/`; flnd's old domain fix-up stays in flnd |
| `pages` | every open tab, as `Page`s |
| `raw` | the Playwright `BrowserContext`, the thing that holds the cookies, storage and pages, in every mode |
| `close()` | the same as leaving the `with` block |

## Page

| Member | Does |
| --- | --- |
| `goto(url, *, idle=False, timeout=30.0) -> bool` | navigates. A navigation error returns `False` and is logged with `log.warn`, instead of raising |
| `on_response(match, handler, *, lines=False)` | `handler(data, response)` for each matching response whose body is JSON. `match` is a URL substring, a compiled regex, or a function. `lines=True` parses newline-delimited JSON, calling the handler once per line. A body that isn't JSON is skipped (logged at `debug`). An exception inside the handler is logged with `log.warn`, with the URL, and later responses keep arriving |
| `on_request(match, handler)` | `handler(request)` for each matching request. Each `on_*` returns a function that stops it |
| `wait_for_selector(css, *, timeout=30.0)`, `wait_for_url(pattern, …)`, `wait_for_load(*, idle=False, …)` (with `idle=True` it also waits until the network is quiet) | wait; all times in seconds |
| `scroll(*, by=None, count=None, limit=None, more=None, idle=4, delay=2.0, max=200, onscroll=None) -> int` | scrolls like a person until the feed ends (see [Scrolling](#scrolling)), `count()` reaches `limit`, `more()` returns `False`, or `max` scrolls are done. `by` is a JS expression or a `(raw_page) -> None` function for sites that need their own scroll. Waits use Playwright's clock, so `on_*` handlers keep running. It returns the number of scrolls |
| `handoff(*, until, message, poll=1.0, timeout=None) -> bool` | for steps only a person can do, such as a captcha or a login. It shows `log.warn(message)` and a spinner, and polls `until()`. It returns `True` when done, or `False` on timeout. In headless mode it raises `RuntimeError`, because no person can see the page |
| `selector` | the live page as a [`selector.Selection`](selector.md), so `page.selector.css("#captcha")` works without `raw.locator`. A property, read fresh each time; it is a snapshot, so a later change to the page needs another read. The HTML is `page.selector.raw.get()`. Needs the `selector` extra |
| `url`, `title` | properties |
| `browser` | the `Browser` it belongs to |
| `raw` | the Playwright `Page`, for `click`, `fill`, `evaluate`, and everything else |
| `close()` | closes the tab |

### Scrolling

`page.scroll` has two jobs: notice reliably when a feed has ended, and not look like a script while it gets there.

**Like a person.** A script that jumps to the bottom every 2 seconds is easy to spot. Each scroll instead:
- moves the mouse to a random point over the page, then turns the **wheel** (`mouse.wheel`), so the site sees real wheel events and its own lazy loading runs;
- goes down by 60–90% of the viewport's height, in 3–6 smaller wheel steps a few tens of milliseconds apart;
- now and then (about one scroll in eight) goes back up a little, as a reader does;
- pauses for `delay` seconds with ±40% jitter, and sometimes (about one scroll in ten) for twice as long.

The randomness comes from one `random.Random`, which a test can seed.

**The end of the feed.** A scroll counts as *progress* when the page grew (`document.scrollingElement.scrollHeight`), `count()` grew, or new responses arrived for an `on_response` handler. When a scroll near the bottom (within 1.5 screens of it) makes no progress, it waits a little longer and jiggles: up a screen and back down, which is what makes most feeds load the next page. The feed has ended after `idle` such scrolls in a row. A scroll higher up that makes no progress doesn't count, because the reader is still going through content that already loaded. A sticky footer or a spinner that never stops can't keep it going forever, because `max` still applies.

`by=` replaces the wheel movement only. The pauses and the end-of-feed rule stay the same.

Downloads, dialogs and other events go through `raw` (`page.raw.on("download", …)`). The two wrappers stop at these lists. Anything rarer goes through `raw`, so they never grow into Playwright's whole API.

## Imports

**Importing `browser` doesn't load Playwright.** Playwright is imported the first time `connect` is called, so `cookies` and the rest of `shared` work without it installed. A test imports `shared.browser` with Playwright blocked and checks that calling `connect` gives the missing-extra `ImportError`.

## Installed profiles

`installed_profile(browser="chrome", profile="Default") -> Path` finds the installed profile folder and returns a temp copy of it, for `connect(profile=…)`.

## Tests

The Playwright tests use local pages and `page.route`. They are skipped when Chromium isn't installed.

- **Imports:** importing `shared.browser` with Playwright blocked works, and `connect` then raises the missing-extra error.
- **`connect`:**
  - incognito leaves nothing behind;
  - a persistent `profile` keeps a cookie between two connects;
  - `installed_profile` copies a fake profile folder, and the copy is deleted on close;
  - `cdp=` attaches to a Chromium the test starts with a debugging port, and leaves it running after disconnecting;
  - `cdp=` with `headless=`, `profile=`, `locale=`, `timezone=`, `args=` or `stealth=` raises `ValueError`;
  - `locale` and `timezone` reach `navigator.language` and `Intl`, incognito and with a profile;
  - headless mode reports the browser's own user agent without "Headless"; a missing channel falls back to the next;
  - `args`;
  - closing after an exception.
- **`Browser`:**
  - `open_page` with and without a URL;
  - `pages` lists every tab;
  - `b.cookies.add`, `all` and `clear_all`;
  - one bad cookie among good ones.
- **`Page`:**
  - `goto` returns `False` on a bad URL;
  - `on_response`: each `match` form, a body that isn't JSON, `lines=True`, a handler exception keeping later events, and `stop()`;
  - each `wait_for_*`, `wait_for_load(idle=True)`, and a timeout;
  - `scroll`: each stopping rule, custom `by`, `onscroll` calls; on a local page that appends items when scrolled near the bottom, it reaches the end and stops; a feed that only loads after the up-and-down jiggle; the wheel is used (the page records `wheel` events), the steps vary, and a seeded run is repeatable;
  - `handoff`: done at once, done later, a timeout, and headless raising;
  - `selector`, `url`, `title`, `browser`, `close`.
