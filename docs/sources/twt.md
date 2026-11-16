# Twitter/X (`twt`)

flnd downloads the photos and videos of an X (Twitter) user: their own posts, their Media tab,
highlights, reposts and replies, and their avatar and banner. It also downloads the media of one
post (a *tweet*) by its link. Timelines are read in a browser by scrolling the profile's tabs;
the profile itself and single tweets are read from X's GraphQL API. A login is not required by
the code, but X usually sends a logged-out browser to its login page, which stops the run. The
source is `flnd/sources/twt.py`.

## Targets

A *target* is what you ask flnd to download (see the [glossary](../glossary.md)). Each form below
is read by one *route*, a method of the source named after the kind of target it reads.

| You type | Route | What it downloads |
| --- | --- | --- |
| `twt:example_handle` | `user` | The profile's categories (below) |
| `https://x.com/example_handle` | `user` | The same; a tab link such as `/media` too |
| `https://x.com/i/user/1234567890` | `user` | The profile with that numeric id |
| `twt:1234567890` | `user` | The profile with that numeric id |
| `https://x.com/example_handle/status/1234567890` | `post` | One tweet's media |
| `https://x.com/i/web/status/1234567890` | `post` | The same (also `x.com/status/<id>`) |

Every `x.com` form also works on `twitter.com`. The `post` patterns are tried before `user`, so
`x.com/status/<id>` is never read as a user named `status`.

## Categories

A *category* is one part of a profile that can be downloaded on its own. Without
[`--only`](../cli.md) a profile gets every category; `--only` names some of them.
`--only` is refused for a `post` target, which is always downloaded whole.

| Category | What it holds |
| --- | --- |
| `posts` | The user's own tweets, retweets left out; newest first |
| `media` | The Media tab: photos in `images/`, videos in `videos/`; newest first |
| `highlights` | The tweets the user highlighted |
| `reposts` | The media of the tweets the user retweeted, dated by the original tweet |
| `replies` | The user's own replies; the tweets they answered are left out; newest first |
| `profile` | The avatar at its original size and the banner at its largest |

The *newest first* categories stop early after 5 tweets with media in a row that are already
saved, or 5 tweets in a row older than the start of `--when` (which covers a pinned tweet).
`highlights` and `reposts` come in the order they were added, so they are read to the end.

Every category but `reposts` leaves retweets out. A tweet's files include the media of a tweet
it quotes and its card's image.

## Logging in

`flnd login twt <how>` takes a browser or a cookie file's path. Left out in a terminal,
the way is asked with numbered choices:

- **From your browser**: reads X's cookies from Chrome, Firefox, Edge, Brave or Chromium.
- **From a cookie file**: reads a Netscape `cookies.txt` or a JSON export.

The login is accepted only when the cookies hold `auth_token`. They are stored encrypted in
`.flnd/secrets.db`, loaded into the HTTP client and the browser each run, and any the
site refreshed are written back at the end. API calls also need the `ct0` cookie, which X sets
and checks as a CSRF token.

Without a login, flnd still tries the API and the pages. When a run starts, flnd warns if
`auth_token` has expired or will expire within a day. When a tab lands on `/i/flow/login`,
`/login` or `/account/access`, the run stops at once with "X asked for a login or locked this
account; check it in a browser or run flnd login twt".

## How it fetches

**The API.** X's web client calls GraphQL operations at
`https://x.com/i/api/graphql/<query id>/<operation>`, with `variables` and `features` as JSON in
the query string. flnd calls three of them over HTTP:

- `UserByScreenName` or `UserByRestId`, once per profile target, for the user's id, avatar and
  banner;
- `TweetResultByRestId` for a `post` target.

Every request to `/i/api/` carries the web client's public bearer token (the `TWITTER_BEARER`
constant), `x-csrf-token` copied from the `ct0` cookie, and `x-twitter-auth-type` when logged in.
The HTTP client sends a desktop Chrome user agent.

Each operation's *query id* comes from the `QUERIES` table in the code. X changes these ids now
and then. When a call is refused with a 400 or 404, flnd reads `https://x.com/` and the web
app's main script on `abs.twimg.com`, takes the current id and feature flags of each operation
from them, keeps them in the source's saved state for later runs, and asks again. It does this
at most once a run.

**The browser.** Each timeline category opens its tab and scrolls it, keeping the JSON of every
response whose URL names the tab's operation:

| Category | Tab | Operation |
| --- | --- | --- |
| `posts` | `https://x.com/<handle>` | `UserOriginalsTimeline` |
| `media` | `/media` | `UserMedia` |
| `highlights` | `/highlights` | `UserHighlightsTweets` |
| `reposts` | `/reposts` | `UserRepostsTimeline` |
| `replies` | `/with_replies` | `UserRepliesTimeline` |

A numeric target opens `https://x.com/i/user/<id>` instead of the handle. The scroll ends when a
page brings no new tweet, or at an early stop. A page of cursors alone, which X sends at a
timeline's end, stops the scroll at once (`timeline_state`, the feed's cursor reader). The tabs not yet read are opened in background
tabs meanwhile. If the API could not find the user, the timelines are still read in the
browser.

A photo is asked for at `name=orig`, its uploaded size, with `4096x4096`, `large` and `medium`
as *mirrors* (addresses tried in turn when one refuses). A video is its highest-bitrate MP4.

**Pace and concurrency.** `pace` is 0.2: at least 0.2 seconds between requests to `x.com` and
`twitter.com`, page loads included. When an API call is answered with HTTP 429, flnd waits
until the time X's `x-rate-limit-reset` header (or `Retry-After`) gives. Each media host's files download
8 at once, adapting up to 32 (no `limits` of its own). The browser runs without flnd's stealth
settings (`stealth = False`).

## Where files go

A target's files go under `.flnd/media/twt/<target>/`, where `<target>` is the handle or id, or
the tweet's id for a `post` target. Inside it, the source declares:

- `folder = "{part}"`: the category's name, except that `media` files go in `images/` or
  `videos/`. A `post` target's files sit directly in the target's folder. The `profile` category
  uses `profile`.
- `filename = "{date}_{id}_{n}"`: a file is `<date>_<tweet id>_<n>.<ext>`, where `n` is the
  media's place in the tweet (`0`, `1`, …), `q0`, `q1`, … for a quoted tweet's media, or `card`
  for the card's image. Without a date the name drops it.
- `key = "{id}_{n}"`: the history remembers `twt:<tweet id>_<n>`. The `profile` category sets
  an empty key, so its history key is the file's path under the target's folder.

The avatar is `avatar_<picture id>.<ext>` and the banner `banner_<banner id>.jpg`.

Example:

```
.flnd/media/twt/example_handle/posts/2026-01-02_1234567890_0.jpg
.flnd/media/twt/example_handle/videos/2026-01-02_1234567891_0.mp4
```

## Quirks

- **Query ids go stale.** X changes its GraphQL query ids; a stale one is answered with a 404
  (an id the API no longer knows) or a 400 (variables or features it no longer takes). flnd
  refreshes them from x.com's main script. Handled since 2026-11-12.
- **API headers only on the API.** x.com's page and script answer 401 to the API's bearer token,
  so the headers are added only to `/i/api/` requests. Handled since 2026-11-12.
- **Two timeline shapes.** A timeline sits under `timeline` in one response shape and
  `timeline_v2` in the other; flnd reads both. Handled since 2026-11-11.
- **Media tab pages.** The Media tab's later pages come as `moduleItems`, not `entries`.
  Handled since 2026-11-11.
- **Limited-visibility tweets** come wrapped in an outer object; flnd reads the tweet inside.
  Handled since 2026-11-11.
- **Tombstones and error pages.** A removed tweet (a *tombstone*) has no id and is never new; a
  response that is an error or throttle body, with no cursor, is skipped and the scroll goes on.
  Handled since 2026-11-16.
- **Dates.** A tweet's `created_at` reads like `Mon Nov 09 10:00:00 +0000 2026`, always UTC. A
  date in another shape leaves the tweet undated instead of failing the timeline. Handled since
  2026-11-16.
- **Avatar size.** An avatar URL ending in `_normal` is the 48 px version; flnd drops it to get
  the picture as uploaded. Handled since 2026-11-11.
- **Banner size.** The banner is asked for at `1500x500`, the largest size the CDN serves.
  Handled since 2026-11-11.
- **Only MP4 videos.** A video's streaming playlists are skipped; only its MP4 variants are
  saved. Handled since 2026-11-11.
- **Login wall paths** are matched as whole path segments, so a handle like `login_example` is
  not a wall. Handled since 2026-11-16.

## Limits

- **Site pages are refused** as "that's not a profile": links under `/home`, `/explore`,
  `/search`, `/settings`, `/notifications`, `/messages`, `/hashtag`, `/i` (except
  `/i/web/status/<id>` and `/i/user/<id>`), `/intent`, `/compose` and `/login`.
- **A single tweet** is read only from the API. If the API refuses it, or the tweet is gone or
  not visible to the login, flnd warns and downloads nothing; there is no browser fallback.
- **If the API cannot find the user**, the `profile` category downloads nothing and `replies`
  keeps nothing, since both need the user's id.
- **Text-only tweets** have no files, so nothing is saved for them.
- **Streaming-only videos** (with no MP4 variant) are skipped.
- **A login wall or locked account** stops the run; it is not waited out.
- **Protected accounts** are readable only when the login can see them.
