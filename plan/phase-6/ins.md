# ins, and API first for every source

Phase 6, step 5, after [session](session.md). It starts with Instagram, then applies the same rule to [every source](#every-source-api-first-the-page-as-fallback). Instagram blocks a run that downloads a large tagged or reposts tab. The grids list most of those posts without their files, so flnd opens each post's page, one after another, as fast as they load. Instagram answers with "Please wait a few minutes", a login wall or a 429. flnd can't see any of these, so it warns "its page holds no post; skipped" and opens the next post at once, which makes the block worse.

## What's wrong today

Checked in code on 2026-11-12:
- **Page loads aren't paced.** `Source.pause` (5 s) is only between targets. Between two post pages there is no pause at all.
- **Page loads aren't checked.** `Page.goto` drops Playwright's response, so a 429, `/accounts/login/` or `/challenge/` counts as loaded. `net.Backoff` covers only httpx: API calls and CDN downloads. ins never calls `session.challenged()`.
- **A throttled GraphQL page counts as progress.** During a scroll, a JSON body such as `{"message": "Please wait a few minutes before you try again.", "status": "fail"}` is ignored by `on_result` but still counts as traffic, so the scroll reaches its idle limit and logs "N posts listed" as if the tab had ended.
- **The WIP commit's scroll delay of 0.2 s** makes the pagination requests come far faster than a person's, and ends a tab after about 2 s with no new response.

## Design

### 1. Read posts through the API, not their pages

For a listed post, ins reads it with **the GraphQL post query its own page sends** (captured once from a post page and [replayed](#three-ways-to-reach-an-api) with each shortcode), because GraphQL's budget is about four times `/api/v1/`'s ([pace](pace.md)). The fallback, when that query can't be captured, is `https://www.instagram.com/api/v1/media/<pk>/info/`. Both return the post in the shape of a post page's `xdt_api__v1__media__shortcode__web_info.items[0]`, so `save_post` and `post_files` are unchanged. One small JSON request replaces a whole page load: no HTML, scripts or images.

- It goes through `session.fetch_json`, so it is paced, its 429 is backed off by the engine, and its cookies are the browser's.
- `Instagram.prepare(request, cookies)` (new, [source](source.md)) adds `X-IG-App-ID: 936619743392459`, `X-CSRFToken: <csrftoken>`, `X-Requested-With: XMLHttpRequest` and `Referer: https://www.instagram.com/`. They go only to `instagram.com` ([api](api.md#source-flndsourcesbasepy)), never to the CDN.
- **httpx's TLS fingerprint differs from Chrome's.** If Instagram turns out to refuse these calls from httpx (to be checked on a live run, [below](#live-checks)), the fallback is the same request from inside the logged-in tab, `page.raw.evaluate("fetch(…)")`, under the same pacing and checks. That stays inside `ins.py`, so it adds no session API.
- A post without a `pk` is still read from its page, under the same pacing and checks.

### 2. Skip before any request

Before fetching a listed post, ins skips it when:
- **it's already saved:** `session.saved(f"{code}_0")`, from the WIP commit. `save_post` keys every file `<code>_<index>`, a single file included, so `_0` is right. A carousel whose later files failed isn't retried this way; the download pool's own retries cover that, and a later `flnd sync` repairs it.
- **its date is outside `--since` / `--until`:** the date comes from the `pk`, before any request. A tagged tab of 2,000 posts with `--since` last month makes only last month's requests.

### 3. Save from the grid only what the grid really has

The WIP commit saves a listed image post straight from the grid, with its date from its `pk`. Kept, with these changes:
- **An allowlist, not a blocklist.** A grid item is saved from the grid only when its `media_type` is 1 (photo) or 8 (carousel) with every child a 1, it has `image_versions2.candidates`, and the tab isn't reels. Today an item without `media_type` is saved as a photo, which on the reels tab saves a video's thumbnail.
- **Only when its largest candidate is full size**: at least 1,080 px on its long side, or its own `original_width`. Otherwise it is read through the API, so nothing is silently saved at thumbnail size. Whether the tagged grid lists full-size images at all is not confirmed yet ([below](#live-checks)).
- `taken_on` catches `OverflowError` and `OSError` as well as `ValueError`, and works in UTC ([fixes](fixes.md#dates)). The `pk` date is used only for grid items, never for stories, where a small `pk` would give 2011.

### 4. See and wait out every throttle

| Sign | Where | ins does |
| --- | --- | --- |
| HTTP 429 | page load, API, CDN | the engine backs off and retries ([session](session.md#members)) |
| a final URL with `/accounts/login/` or `/challenge/` | page load | ins checks `session.page.raw.url` after `goto`, calls `session.throttled("login wall")`, and loads the page again |
| `"status": "fail"` with "wait a few minutes", or `"require_login": true` | API and GraphQL bodies, often with a 400 or 401 | `session.throttled("Instagram asked to wait")`, then the same request again |

- **A throttle is never a skip.** The post is retried after the wait, as long as it takes. That is the rule flnd already follows for a 429: [rate limits](../phase-4/engine.md) are waited out, never given up on.
- **During a scroll, a throttled GraphQL response stops the scroll's end rule.** ins's handler calls `session.throttled`, which waits, and the scroll's 10 s without progress starts again after the wait. So a throttle can't make a tab look finished.

### 5. Pace

Instagram's pace is its known budgets: 275 GraphQL calls per 10 minutes and 75 `/api/v1/` calls per 11 minutes, with a 1 s floor ([pace](pace.md)). That makes a 2,000-post tagged tab about 10–25 minutes of requests instead of hours. The WIP commit's fixed 0.2 s scroll goes. Scrolling, now only the fallback, waits for each page of posts to arrive and then scrolls again after a learned floor of at least 0.3 s ([session](session.md#scrolling-on-the-pages-own-time)).

## Every source: API first, the page as fallback

Instagram's fix is one case of a general rule, which every source follows from Phase 6: **read a site's own API whenever it can be reached, and load or scroll a page only when it can't.** An API call is one small JSON response. A page load or a scroll fetches HTML, scripts, images and video previews, and makes every request a page makes, so it is slower, heavier, and what rate limits count.

### Three ways to reach an API

| Way | How | Used when |
| --- | --- | --- |
| **direct** | `session.fetch_json(url)` through httpx, with the login's cookies and the source's `prepare` headers | the API answers a plain client: loi today, twt today, ins's media info if the [live check](#live-checks) passes |
| **in page** | `session.fetch_json(url, in_page=True)`: the same request sent by the logged-in tab's own `fetch()` | the API wants the browser's TLS fingerprint, cookies set by scripts, or headers only the page knows |
| **replay** | the source watches the first API request the page makes (`on_response` gives the `response`, and `response.request` its URL, body and headers), then sends that same request **in page**, changing only its cursor | the request needs tokens the page computes (Facebook's `fb_dtsg` and `doc_id`, TikTok's signed parameters) |

**`fetch_json(…, in_page=True)`** is one new keyword, not a new member ([session](session.md#members)):
- It goes through `shared` `Page.fetch(url, *, method="GET", headers=None, body=None) -> (status, headers, text)`, a small new `browser` method (about 20 lines over `page.raw.evaluate`) that `shared` judges useful on its own ([api](api.md#browser-529-lines)).
- It has the same pacing, 429 back-off, `soft=` and `FetchError` as a direct call. So a source picks the way by one keyword, and everything else stays the same.
- It needs an open page, so the first `goto` of the run opens one. A source that only replays still loads its first page normally.

**Replay** stays inside each source, since each site's cursor is in a different place. It is a short loop:

```python
first = profile.first_request                      # kept by the on_response handler
while cursor and not session.full:
    data = session.fetch_json(first.url, method=first.method, in_page=True,
                              data=with_cursor(first.post_data, cursor), headers=first.headers)
    cursor = profile.on_result(session, data, None)
```

**Fallback.** When an API way fails in a way that isn't a rate limit (a 400 or 404 from a stale query ID, a signature the site refuses, a shape the source doesn't know), the source warns once (`fbk: timeline API refused; scrolling instead`) and scrolls the page as it does today. A rate limit is waited out, never a reason to fall back.

### Per source

Checked in code on 2026-11-12. "Live" means the way is confirmed by hand on a test account before the step is done.

| Source | Today | Phase 6 |
| --- | --- | --- |
| **ins** | grids scrolled; each incomplete post's page opened | grids by **replay** of the profile's GraphQL pagination (the `xdt_api__v1__feed__user_timeline_graphql_connection` query and the tagged, reels and reposts ones), with `after` as the cursor; posts by **direct** or **in-page** media info ([above](#1-read-posts-through-the-api-not-their-pages)); stories and highlights by `/api/v1/feed/reels_media/?reel_ids=…`, **in page**, one call for all of them. The profile page is loaded once, for its first request and its embedded first results. Live |
| **fbk** | the timeline, photos and albums scrolled while `graphql` responses are read | **replay** of the timeline's and photos tab's `/api/graphql/` POST: the first request's `doc_id`, `fb_dtsg`, `lsd` and `variables`, with `cursor` in the variables changed. Stories stay on their page: there is one per profile, and its page holds them all. Live |
| **twt** | **direct** GraphQL (`UserTweets`, `UserMedia`, …) with built-in query IDs; the browser only when the API refuses | — the same, plus: when a query ID is stale (404 or 400), the source reads the current IDs once from the page's main script, which the browser fallback has open, and tries the API again before scrolling |
| **tik** | the profile scrolled while `/api/post/item_list` and the other lists are read | **replay** of each list's first request, **in page**, with `cursor` changed. TikTok signs its list URLs (`X-Bogus`, `msToken`) with the page's own script. Whether a replayed URL with a new cursor is still accepted is the open question, so scrolling stays as the fallback. Live |
| **loi** | **direct** API | — |
| **tlg** | Telethon, Telegram's own API | — |

### What it costs and gives

- **Lines:** about 25 per browser source for the replay loop and the first-request capture, about 75 in all. That is offset by the scroll counters and end rules they no longer need on the main path. The scroll code stays as the fallback, so the [scripts](scripts.md) estimates rise by about 10 a source.
- **Requests:** a tagged tab of 2,000 posts takes about 170 GraphQL pages of 12. Today that's about 2,000 post-page loads on top of the scroll; after, 170 small JSON calls plus the media info calls for the posts that need them.
- **Pace:** API calls follow each source's [pace](pace.md): its headers, its known budgets, and a learned gap with a floor. "Faster" means fewer and smaller requests, each as soon as the site allows.

## Live checks

These are checked once, by hand, on the user's authorized test account, before the step is called done. Nothing live goes into the tests.
- whether `/api/v1/media/<pk>/info/` answers httpx with the browser's cookies, or needs the in-page fetch;
- whether the tagged and reposts grid items carry `pk`, `media_type` and full-size `image_versions2`;
- a full tagged and reposts run of the test account finishes with no "holds no post" warnings and with every throttle waited out.
- for each of ins, fbk and tik: the replayed pagination returns the same posts as scrolling, for the test account, and a stale or refused replay falls back to scrolling with one warning;
- twt: a stale query ID is refreshed from the page's script.

## Tests

`tests/test_ins.py`, with anonymized fixtures only:
- a tagged post is read through the media info API, not its page; a post without a `pk` through its page;
- a post already saved, or dated outside `--since`, makes no request;
- a grid photo without `media_type`, a reels-tab item, and a photo smaller than 1,080 px are read through the API, not saved from the grid;
- a "Please wait a few minutes" body calls `throttled` and then retries the same post, which is saved;
- a page load that ends on `/accounts/login/` is waited out and loaded again;
- a throttled GraphQL response during the scroll doesn't end the tab;
- `taken_on` with a huge `pk` gives None, and a story without `taken_at` isn't dated from its `pk`.
- `fetch_json(in_page=True)` sends through `Page.fetch`, with the same pacing, back-off and `FetchError` as a direct call (with a fake page);
- a replay that gets a 400 warns once and falls back to `scroll`; one that gets a 429 waits and replays again.
