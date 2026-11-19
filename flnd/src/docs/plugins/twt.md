# Twitter/X (`twt`)

flnd downloads the photos and videos of an X (Twitter) user: their own posts, their Media tab,
highlights, reposts and replies, and their avatar and banner. It also downloads the media of one
post (a *tweet*) by its link, and the tweets of a search. Timelines and searches are read in a
browser by scrolling their tabs. The profile and single tweets are read from X's GraphQL API. The
login is optional, but X usually sends a logged-out browser to its login page, which fails the
job. The plugin is `src/plugins/twt.py`, on plugin API 2.

## Targets

A *target* is what you ask flnd to download (see the [glossary](../glossary.md)). Each form below
is read by one *route*, a method of the plugin named after the kind of target it reads.

| You type | Route | What it downloads |
| --- | --- | --- |
| `twt:example_handle` | `user` | The profile's categories (below) |
| `https://x.com/example_handle` | `user` | The same; a tab link such as `/media` too |
| `https://x.com/i/user/1234567890` | `user` | The profile with that numeric id |
| `twt:1234567890` | `user` | The profile with that numeric id |
| `https://x.com/example_handle/status/1234567890` | `post` | One tweet's media |
| `https://x.com/i/web/status/1234567890` | `post` | The same (also `x.com/status/<id>`) |
| `twt?example words` | `search` | Each tweet with media the search's Latest tab lists |

Every `x.com` form also works on `twitter.com`. The `post` patterns are tried before `user`, so
`x.com/status/<id>` is never read as a user named `status`.

**Search.** `twt?<words>` opens `https://x.com/search?q=<words>&src=typed_query&f=live` (the
Latest tab) and scrolls it, reading the `SearchTimeline` responses. X's own operators (`from:`,
`filter:images`, `filter:media`, …) work in the words; for example,
`twt?@example_handle -from:example_handle` fetches media from other people's tweets
mentioning `example_handle` ([cookbook](../cookbook.md#twitterx-tagged-and-mentioned-media)).
X's `filter:media` and `filter:images` index drops many tweets, while flnd already skips text-only
tweets locally, so leaving `filter:media` out is more complete.
`--when` is also sent to X as `since:` and `until:`
operators (`until:` is the day after `--when`'s last, since X's is exclusive), so a short range
doesn't scroll through years and older history can be sliced year by year. When a search window
stops at X's cursor limit (before reaching `--when`'s start date), flnd automatically opens a
follow-up window with `max_id:<oldest_id - 1>` (counting text-only tweets too, and replacing any
existing `max_id:` operator) until a clean window finds no older tweets. Transient empty pages
with advancing `Bottom` cursors are scrolled past (up to 3 in a row), and HTTP 429 rate limits
hold the `SearchTimeline` bucket for `x-rate-limit-reset` or `Retry-After` before resuming. A
search is a query
target: it yields a tweet link (`https://x.com/i/web/status/<id>`) for each tweet with a file.
The engine downloads each as a
`post` target, into its own folder `twt/<tweet id>/`, named and keyed as a tweet link is. Pass
`--folder=example_handle` or `--folder=example_handle/tagged` (or `--folder="{query}"`) to
collapse every discovered tweet into one folder under `.flnd/media/twt/<folder>/` instead. The
tweets a search lists are kept for the run, so the `post` target for each one costs no request.
A search needs a login: a logged-out browser lands on X's login page, which is waited out and then
fails the job.

## Categories

A *category* is one part of a profile that can be downloaded on its own. Without categories
([the CLI guide](../cli.md)) a profile gets every category but `reposts`. `twt:<user>/media` names
some of them, and `/all` takes `reposts` too. A `post` target takes none: it is always downloaded
whole.

| Category | What it holds |
| --- | --- |
| `posts` | The user's own tweets, retweets left out; newest first |
| `media` | The Media tab: photos in `images/`, videos in `videos/`; newest first |
| `highlights` | The tweets the user highlighted |
| `reposts` | The media of the tweets the user retweeted, dated by the original tweet |
| `replies` | The user's own replies; the tweets they answered are left out; newest first |
| `profile` | The avatar at its original size and the banner at its largest |

The *newest first* categories stop early, by the engine's rules: after 5 saved posts in a row, or
after 5 posts in a row older than the start of `--when`. A pinned tweet does not count toward the
older stop. The plugin yields every tweet in listing order, and the engine closes the scroll.
`highlights` and `reposts` come in the order they were added, not by date, so they are read to
the end.

Every category but `reposts` leaves retweets out. A tweet's files include the media of a tweet
it quotes, and its card's image. `reposts` is marked `own=False`, so it is taken only when named
(see above).

## Logging in

`flnd auth login twt <method>` logs in. The methods are `browser`, `chrome`, `file <path>`,
`firefox` and `safari`, the cookie methods the plugin declares (`cookies("auth_token")` in
`twt.py`). Left out in a terminal, flnd asks for the method from a menu, and asks for the path when
the method is `file`.

- **`browser`**: opens a window of flnd's own Chromium at X's login page. Log in there, and flnd
  reads the cookies once `auth_token` is set. Close the window to give up.
- **`chrome`**: reads the cookies from your Chrome through flnd's Chrome extension.
- **`file <path>`**: reads a Netscape `cookies.txt` or a JSON export of cookies.
- **`firefox`** and **`safari`**: read the cookies from that browser. Safari is read on macOS
  only.

The login is accepted only when the cookies hold `auth_token`. Only the cookies for `x.com` and
`twitter.com` are kept. The login is sealed in the vault, `metadata/secret.db` in the home folder.
The key that opens it is kept in the user's key file, outside the home. Each run loads the cookies
into its HTTP client and its browser, and at the end writes back any of them the site refreshed.
API calls also need the `ct0` cookie, which X sets, and which the plugin sends back as the CSRF
token.

Without a login, flnd still tries the API and the pages. At the start of a run, flnd warns if
`auth_token` has expired, or expires within 3 days. When a tab lands on one of the plugin's walls
(`/i/flow/login`, `/login` or `/account/access`), the account's requests wait 30 seconds, then 120
seconds, and the page is loaded again. After the second wait the job moves to another account if
there is one, or fails with a login error and the account is marked expired.

## How it fetches

**The API.** X's web client calls GraphQL operations at
`https://x.com/i/api/graphql/<query id>/<operation>`, with `variables` and `features` as JSON in
the query string. flnd calls three of them over HTTP:

- `UserByScreenName` or `UserByRestId`, once per profile target, for the user's id, avatar and
  banner;
- `TweetResultByRestId` for a `post` target.

The plugin's `sign` hook adds to every request to `/i/api/`, before each try, the web client's
public bearer token (the `TWITTER_BEARER` constant), `x-csrf-token` copied from the `ct0` cookie,
and `x-twitter-auth-type` when logged in. The HTTP client sends a desktop Chrome user agent.

Each operation's *query id* comes from the `QUERIES` table in the code. X changes these ids now
and then. When a call is refused with a 400 or 404, flnd reads `https://x.com/` and the web app's
main script on `abs.twimg.com`. It takes the current id and feature flags of each operation from
them, keeps them in the plugin's saved state for later runs, and asks again. It does this at most
once a run.

Each operation is a bucket of its own. A 429 holds only that operation, for the wait X names, or
the engine's 30 seconds when it names none. The other operations are not held.

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
timeline's end, stops the scroll at once (`timeline_state`, the feed's cursor reader). Each
timeline's `url=` names its tab, so the engine opens the tabs ahead in background tabs while the
run's tab budget has room. If the API could not find the user, the timelines are still read in
the browser.

A photo is asked for at `name=orig`, its uploaded size. `4096x4096`, `large` and `medium` are its
*mirrors*, addresses tried in turn when one refuses. A video is its highest-bitrate MP4.

**Pace and concurrency.** `pace` is 0.2: at least 0.2 seconds between two requests of one bucket
of one account. Each media host's files download 8 at once, adapting up to 32 (the plugin sets no
`limits` of its own). Stealth is off (`stealth = False`), since this browser never hid
`navigator.webdriver`.

## Where files go

A target's files go under `.flnd/media/twt/<target>/`, where `<target>` is the handle or id, or
the tweet's id for a `post` target. Inside it, the plugin declares:

- `folder = "{part}"`: the category's name, except that `media` files go in `images/` or
  `videos/`. A `post` target's files sit directly in the target's folder. The `profile` category
  uses `profile`.
- `filename = "{date}_{post}_{n}"`: a file is `<date>_<tweet id>_<n>.<ext>`, where `n` is the
  media's place in the tweet (`0`, `1`, …), `q0`, `q1`, … for a quoted tweet's media, or `card`
  for the card's image. Without a date the name drops it.
- `key = "{post}_{n}"`: the history remembers `twt:<tweet id>_<n>`. The `profile` category keys
  by the file's path, so its history key is that path under the target's folder.

The avatar is `avatar_<picture id>.<ext>` and the banner `banner_<banner id>.jpg`.

Example:

```
.flnd/media/twt/example_handle/posts/2026-01-02_1234567890_0.jpg
.flnd/media/twt/example_handle/videos/2026-01-02_1234567891_0.mp4
```

With `--folder=example_handle/tagged`, every file from the run (including every tweet a search
discovers) saves directly under `.flnd/media/twt/example_handle/tagged/<filename>` instead of
separate `twt/<target>/` folders.

## Quirks

- **Query ids go stale.** X changes its GraphQL query ids. A stale one is answered with a 404 (an
  id the API no longer knows) or a 400 (variables or features it no longer takes). flnd refreshes
  them from x.com's main script, as described above.
- **API headers only on the API.** x.com's page and script answer 401 to the API's bearer token,
  so the headers are added only to `/i/api/` requests.
- **Two timeline shapes.** A timeline sits under `timeline` in one response shape and
  `timeline_v2` in the other, and flnd reads both.
- **Media tab pages.** The Media tab's later pages come as `moduleItems`, not `entries`.
- **Limited-visibility tweets** come wrapped in an outer object. flnd reads the tweet inside.
- **Tombstones and error pages.** A removed tweet (a *tombstone*) has no id, so it is never new. A
  response that is an error or throttle body, with no cursor, is skipped, and the scroll goes on.
  An HTTP 429 response on a feed (`SearchTimeline` or a profile timeline) holds that operation's
  bucket for `x-rate-limit-reset` or `Retry-After` and resumes cleanly (failing only after
  repeated holds without progress).
- **Search cursor limits and empty pages.** X search can stop scrolling mid-history or return
  empty pages while advancing the `Bottom` cursor. flnd continues across up to 3 consecutive empty
  search pages when the cursor advances, and chains `max_id:<oldest_id - 1>` windows until a
  clean window returns no older tweets (or reaches `--when`'s start).
- **Dates.** A tweet's `created_at` reads like `Mon Nov 09 10:00:00 +0000 2026`, always UTC. A
  date in another shape leaves the tweet undated, instead of failing the timeline.
- **Avatar size.** An avatar URL with `_normal` in it is the 48 px version. flnd drops that to get
  the picture as uploaded.
- **Banner size.** The banner is asked for at `1500x500`, the largest size the CDN serves.
- **Only MP4 videos.** A video's streaming playlists are skipped. Only its MP4 variants are saved.
- **Login wall paths** are matched as whole path segments, so a handle like `login_example` is not
  a wall.
- **Replies without the API.** When the API can't find the user, `replies` tells the user's own
  replies from the tweets they answered by each tweet's author: its screen name, compared with the
  handle, or the user id for a numeric target.

## Limits

- **Site pages are refused** as "that's not a profile": links under `/home`, `/explore`,
  `/search`, `/settings`, `/notifications`, `/messages`, `/hashtag`, `/i` (except
  `/i/web/status/<id>` and `/i/user/<id>`), `/intent`, `/compose` and `/login`.
- **A single tweet** is read only from the API, or from a search of the same run. There is no
  browser fallback. The plugin doesn't tell a protected tweet from a deleted one: both are
  `NotFound`, and the target fails. A refused API call fails with its own error.
- **If the API cannot find the user**, the `profile` category fails, since the pictures come from
  the API's user object. If X refused the lookup with an error, `profile` fails with that error
  instead. A login refusal (401 or 403) fails the job with a login error. Either way the
  timelines, `replies` included, are still read in the browser.
- **Text-only tweets** have no files, so nothing is saved for them.
- **Streaming-only videos** (with no MP4 variant) are skipped.
- **A login wall or a locked account** is waited out as described above, then the job fails with a
  login error or moves to another account.
- **Protected accounts** are readable only when the login can see them.

## Testing

Unit tests fake the network, the browser and the clock. Live checks are done by hand, on a test
account, and never in unit tests. A change to this plugin must keep the routes and link forms
above, the `own` flag and `newest_first` order of each category, the wall and block waits, and the
history keys (`twt:<tweet id>_<n>`, `{path}` for the profile pictures), so that files already saved
are not saved again.
