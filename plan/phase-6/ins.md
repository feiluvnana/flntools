# ins

Phase 6, step 5, after [session](session.md). Instagram blocks a run that downloads a large tagged or reposts tab. The grids list most of those posts without their files, so flnd opens each post's page, one after another, as fast as they load. Instagram answers with "Please wait a few minutes", a login wall or a 429. flnd can't see any of these, so it warns "its page holds no post; skipped" and opens the next post at once, which makes the block worse.

## What's wrong today

Checked in code on 2026-11-12:
- **Page loads aren't paced.** `Source.pause` (5 s) is only between targets. Between two post pages there is no pause at all.
- **Page loads aren't checked.** `Page.goto` drops Playwright's response, so a 429, `/accounts/login/` or `/challenge/` counts as loaded. `net.Backoff` covers only httpx: API calls and CDN downloads. ins never calls `session.challenged()`.
- **A throttled GraphQL page counts as progress.** During a scroll, a JSON body such as `{"message": "Please wait a few minutes before you try again.", "status": "fail"}` is ignored by `on_result` but still counts as traffic, so the scroll reaches its idle limit and logs "N posts listed" as if the tab had ended.
- **The WIP commit's scroll delay of 0.2 s** makes the pagination requests come far faster than a person's, and ends a tab after about 2 s with no new response.

## Design

### 1. Read posts through the API, not their pages

For a listed post with a `pk`, ins fetches `https://www.instagram.com/api/v1/media/<pk>/info/`. Its `items[0]` has the same shape as a post page's `xdt_api__v1__media__shortcode__web_info.items[0]`, so `save_post` and `post_files` are unchanged. One small JSON request replaces a whole page load: no HTML, scripts or images.

- It goes through `session.fetch_json`, so it is paced, its 429 is backed off by the engine, and its cookies are the browser's.
- `Instagram.prepare(request, cookies)` (new, [source](source.md)) adds `X-IG-App-ID: 936619743392459`, `X-CSRFToken: <csrftoken>`, `X-Requested-With: XMLHttpRequest` and `Referer: https://www.instagram.com/`. They go only to `instagram.com` ([api](api.md#source-flndsourcesbasepy)), never to the CDN.
- **httpx's TLS fingerprint differs from Chrome's.** If Instagram turns out to refuse these calls from httpx (to be checked on a live run, [below](#live-checks)), the fallback is the same request from inside the logged-in tab, `page.raw.evaluate("fetch(…)")`, under the same pacing and checks. That stays inside `ins.py`, so it adds no session API.
- A post without a `pk` is still read from its page, under the same pacing and checks.

### 2. Skip before any request

Before fetching a listed post, ins skips it when:
- **it's already saved:** `session.downloaded(f"{code}_0")`, from the WIP commit. `save_post` keys every file `<code>_<index>`, a single file included, so `_0` is right. A carousel whose later files failed isn't retried this way; the download pool's own retries cover that, and a later `flnd sync` repairs it.
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

`Instagram.pace = 3.0`: at least 3 s, ±40%, between two API calls or page loads, so a 2,000-post tagged tab takes about 2 hours of requests, not a burst of 2,000. The same `pace` is the pause between scrolls ([source](source.md)); the WIP commit's 0.2 s goes.

## Live checks

These are checked once, by hand, on the user's authorized test account, before the step is called done. Nothing live goes into the tests.
- whether `/api/v1/media/<pk>/info/` answers httpx with the browser's cookies, or needs the in-page fetch;
- whether the tagged and reposts grid items carry `pk`, `media_type` and full-size `image_versions2`;
- a full tagged and reposts run of the test account finishes with no "holds no post" warnings and with every throttle waited out.

## Tests

`tests/test_ins.py`, with anonymized fixtures only:
- a tagged post is read through the media info API, not its page; a post without a `pk` through its page;
- a post already saved, or dated outside `--since`, makes no request;
- a grid photo without `media_type`, a reels-tab item, and a photo smaller than 1,080 px are read through the API, not saved from the grid;
- a "Please wait a few minutes" body calls `throttled` and then retries the same post, which is saved;
- a page load that ends on `/accounts/login/` is waited out and loaded again;
- a throttled GraphQL response during the scroll doesn't end the tab;
- `taken_on` with a huge `pk` gives None, and a story without `taken_at` isn't dated from its `pk`.
