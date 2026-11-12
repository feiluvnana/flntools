# flows

Phase 6, with [ins](ins.md) (step 5) and [scripts](scripts.md) (step 6). The user asked on 2026-11-12 to optimize flnd's flows using what tools like gallery-dl have learned. This file records what gallery-dl does for the four browser-era sites, as read on 2026-11-12 in its GitHub master (1.32.19) and its Codeberg development copy (1.33.0-dev), where development has moved. It then lists what Phase 6 takes from it. Nothing was sent to the sites to write this.

**The main lesson:** gallery-dl reaches all four sites with plain HTTP requests, never a browser. It sends a browser's headers in their order, and that browser's TLS cipher list. So flnd's [API-first](ins.md#every-source-api-first-the-page-as-fallback) order is right, and for some lists the **direct** way may work before the in-page or replay ways are needed.

## What gallery-dl does

| Site | Lists | Per post | Best file | Blocks |
| --- | --- | --- | --- | --- |
| **ins** (`InstagramAPI`) | GraphQL POST `/graphql/query` with a `doc_id`, `fb_dtsg` and `lsd`, 12 per page, cursor `after` ← `end_cursor`. Tokens come from the profile HTML's `__eqmc` script; `doc_id`s and relay-provider variables are matched in `rsrc.php` scripts and cached for 24 h, with a built-in table as the fallback. Stories and highlights: GraphQL, 5 reels per call | a grid item with `product_type == "feed"` is used as it is; anything else costs one `/api/v1/media/<pk>/info/` | image `candidates[0]`, warning when it's more than 10% under `original_width`; video `video_versions[-1]`, or the DASH manifest; a manifest at 1/1 fps is a still photo | a redirect to `/accounts/login/` or `/challenge/` stops; 6–12 s before every request |
| **twt** (`TwitterAPI`) | GraphQL GET, **50** per page, about 35 `features` flags plus `fieldToggles`; `UserOriginalsTimeline`, `UserRepliesTimeline`, `UserRepostsTimeline`, `UserHighlightsTweets`, and Media now as `UserPhotoTimeline` + `UserVideoTimeline`. It also sends an `x-client-transaction-id` computed from the homepage and one script, cached 3 h | — | images `?format=jpg&name=orig`, then `4096x4096`, `large`; videos the highest `bitrate` | remaining < 6 waits for the reset; "temporarily locked" stops |
| **tik** | plain GETs, **no signing**: `creator/item_list` (cursor: the last item's `createTime`), `repost/`, `favorite/`, `user/collect/`, `story/item_list`, with fixed web parameters (`aid=1988`, `app_name=tiktok_web`, a random 19-digit `device_id`, `verifyFp`). A repeated page gets a new `device_id`. It answers TikTok's JS challenge (a SHA-256 proof of work) itself | each post's page again (flnd doesn't need this: the list items have the files) | `bitrateInfo` by pixel count, skipping DASH; `playAddr` last | 403s and an empty first page are known problems |
| **fbk** | no GraphQL: one HTML page per photo, following `nextMediaAfterNodeId`; only the 8 albums a tab preloads | — | `"image":{"uri"}`; video: the MPD's largest `FBQualityLabel` with its audio, else `browser_native_hd_url` | a `/login` redirect, or `{"__dr":"CometErrorRoot.react"}` in the body, is "temporarily blocked", and stops |

What breaks often, from gallery-dl's issue tracker:
- **Instagram's `doc_id`s and their required variables change.** They broke posts twice in October 2026 (#9816, #9829), and stories are breaking now (Codeberg #557).
- **X's transaction-id script format changes** (#9602).
- **TikTok answers 403 to many posts** (#9796), and its first list page sometimes comes back empty (#9718).
- **Facebook silently misses images** (#8511, #8973).

## What Phase 6 takes

In order of value. "Live" means checked by hand on a test account before the step is done.

1. **tik: the best video is `bitrateInfo`'s largest.** Today `tik.best_video` prefers `playAddr`, then `downloadAddr`, which is **watermarked**, and uses `bitrateInfo` last. It becomes: `bitrateInfo` sorted by width × height and then `Bitrate`, skipping DASH entries, then `playAddr`, and never `downloadAddr`. This is a bug fix ([fixes](fixes.md#tiktok-quality)).
2. **tik: the list APIs direct first.** `creator/item_list` and the other lists are fetched through httpx with gallery-dl's fixed parameters and a random `device_id`, made anew when a page repeats. The order is then in-page, replay, and scrolling last. That likely answers [ins](ins.md#per-source)'s open question about signed URLs, and takes the browser off TikTok's main path. Live.
3. **ins: replay, not built-in query IDs, for the profile lists.** Replay sends whatever `doc_id` and relay variables the page sends today, so the churn that broke gallery-dl twice in October can't break flnd. This is already the plan ([ins](ins.md#per-source)).
4. **ins: `product_type == "feed"` decides that a grid item is complete**, with gallery-dl's check that `candidates[0]` is within 10% of `original_width`. Everything else goes through `/api/v1/media/<pk>/info/`, as gallery-dl does. That replaces the planned `media_type` allowlist and 1,080 px rule in [ins § 3](ins.md#3-save-from-the-grid-only-what-the-grid-really-has).
   - The "GraphQL post query" planned in [ins § 1](ins.md#1-read-posts-through-the-api-not-their-pages) stays an experiment, because gallery-dl never uses one. If it is accepted live, it's used for the larger GraphQL [budget](pace.md); otherwise media info is used.
5. **ins: the headers gallery-dl sends.**
   - `prepare` adds `X-IG-WWW-Claim`, kept from each response's `x-ig-set-www-claim`, and `X-ASBD-ID`.
   - For GraphQL it also adds `X-FB-LSD` and `X-FB-Friendly-Name`.
   - Fewer direct calls are then refused.
6. **ins: stories and highlights, at most 5 reels per call**, with the story page as the fallback. That replaces "one call for all of them" in [ins](ins.md#per-source).
7. **twt: ask for what X's own web app asks for.**
   - gallery-dl's full `features` and `fieldToggles`, and `count: 50` instead of 20, which means 2.5× fewer requests.
   - When `UserMedia` is refused, `UserPhotoTimeline` and `UserVideoTimeline`.
   - The `x-client-transaction-id` is added only if a live check shows timelines need it. It is about 80 fragile lines, and refreshing query IDs from the page's script ([ins](ins.md#per-source)) comes first.
8. **fbk: its block is seen, not mistaken for an ended tab.** A `/login` final URL, or `CometErrorRoot.react` in a body, calls `session.throttled` on the replay and photo paths. flnd's planned GraphQL replay is already ahead of gallery-dl's one-page-per-photo walk, so nothing else changes.
9. **Every direct source sends a browser's header order and a matching cipher list**, like gallery-dl's `browser` option: Chrome's for every source, matching the one user agent of [source](source.md#what-goes-and-why). So fewer direct calls are refused before the in-page way is needed. About 25 lines in `flnd/net.py`.
10. **ins: `/accounts/suspended/` is a hard stop.** It is the account itself in trouble, not a rate limit, so waiting would only make it worse. The run stops for that source with `ins: Instagram suspended or locked this account; check it in a browser`.
    - `/challenge/` and `/accounts/login/` are still [waited out](ins.md#4-see-and-wait-out-every-throttle), as decided in Phase 4: a rate limit or challenge never stops flnd.
    - X's "temporarily locked" is treated like `/accounts/suspended/`.

Not taken:
- **TikTok's JS proof-of-work.** flnd has a real browser for when the challenge comes, and the user decided in Phase 4 that a person solves TikTok's captcha.
- **Re-reading every TikTok post's page.** The list items already hold the files.
- **gallery-dl's fixed 6–12 s for Instagram.** The [pace](pace.md) budgets are both faster and closer to Instagram's real limits.
- **Facebook's one-page-per-photo walk.** It makes one page load per photo, which is exactly what this phase removes.

## Not verified

To be settled by the live checks:
- whether Instagram accepts a GraphQL post query;
- whether TikTok's list APIs answer httpx today without signing;
- whether X now requires `x-client-transaction-id` for timelines;
- whether 5 really is a cap on reels per call;
- how much the cipher list changes what the sites accept.
