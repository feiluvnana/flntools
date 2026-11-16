# Instagram (`ins`)

flnd downloads a profile's posts, reels, tagged posts, reposts, active stories, highlights and
profile picture from Instagram, or one post or reel by its link. It can also list every account
the logged-in user follows. It always uses a browser: profile tabs are read by scrolling a real
page. A login is not required by the code, but in practice Instagram shows little to a
logged-out session, and the `ins:me` target cannot work without one. The source is
`flnd/sources/ins.py`.

## Targets

A *target* is what you ask flnd to download (see the [glossary](../glossary.md)). Each form
below is read by one *route*, a method of the source named after the kind of target it reads.

| You type | Route | What it downloads |
| --- | --- | --- |
| `ins:example_user` | `user` | The profile's categories (below) |
| `https://www.instagram.com/example_user/` | `user` | The same; so is a tab link (`/reels/`) |
| `https://www.instagram.com/p/Cexample123/` | `post` | One post, every file of it |
| `https://www.instagram.com/reel/Cexample123/` | `post` | One reel; `/reels/`, `/tv/` too |
| `https://www.instagram.com/example_user/p/Cexample123/` | `post` | One post, under a profile |
| `ins:me` | `me` | Every account the login follows, each as its own `user` target |

A post's *code* is the short text in its link. The `post` patterns are tried before `user`, so
`instagram.com/p/<code>` is never read as a user named `p`.

## Categories

A *category* is one part of a profile that can be downloaded on its own. Without
[`--only`](../cli.md) a profile gets every category; `--only` names some of them.
`--only` is refused for a `post` target, which is always downloaded whole. For `ins:me`,
`--only` applies to each followed account it finds.

| Category | What it holds |
| --- | --- |
| `posts` | The posts grid, newest first, to its end |
| `reels` | The reels tab, newest first |
| `tagged` | Posts the profile is tagged in |
| `reposts` | Posts the profile reposted, newest first |
| `stories` | The stories active now |
| `highlights` | Every highlight, each in its own folder |
| `profile` | The profile picture, at its largest size |

`posts`, `reels` and `reposts` are *newest first*: a listing stops early after 5 posts in a row
that are already saved, or 5 in a row older than the start of `--when`. `tagged` is always read
to its end.

## Logging in

`flnd login ins <how>` takes a browser or a cookie file's path. Left out in a terminal,
the way is asked with numbered choices:

- **From your browser**: reads Instagram's cookies from Chrome, Firefox, Edge, Brave or
  Chromium.
- **From a cookie file**: reads a Netscape `cookies.txt` or a JSON export.

The login is accepted only when the cookies hold `sessionid`. They are stored encrypted in
`.flnd/metadata/secrets.db`. Each run loads them into its HTTP client and its browser, and at the
end writes back any of those cookies the site refreshed.

Without a login, flnd still tries: it opens the same pages, and gets what Instagram shows a
logged-out visitor. `ins:me` needs the logged-in account's id, read from the `ds_user_id` or
`sessionid` cookie or from `/api/v1/accounts/current_user/`; without one it stops with
"not logged in; run flnd login ins".

When a run starts, flnd warns if `sessionid` has expired or will expire within a day. When the
session stops working mid-run:

- a page or API call sent to `/accounts/login` or `/challenge` (a *login wall*) is waited out
  and tried again, at most twice, then the run stops with a login error;
- a page or call sent to `/accounts/suspended` stops at once: "Instagram suspended or locked
  this account; check it in a browser".

## How it fetches

The source mixes three ways of reading:

1. **Embedded results.** An Instagram page carries GraphQL results inside
   `<script type="application/json">` blocks (the ones holding `__bbox`). flnd reads these
   first when a page loads.
2. **Captured responses.** While a profile tab scrolls, flnd keeps the JSON of every response
   whose URL contains `graphql`, and reads the posts from them. It scrolls until the tab's
   `page_info` says there is no next page (`page_state`, the feed's cursor reader), which stops
   the scroll at once, with no wait for silence.
3. **The REST API** at `https://www.instagram.com/api/v1/`, the one the web client uses:
   - `media/<pk>/info/` for one post (`pk` is the post's numeric id, decoded from its code);
   - `feed/user/<id>/story/` for a user's active stories;
   - `feed/reels_media/?reel_ids=…` for highlights, at most 5 per call;
   - `friendships/<id>/following/?count=100` for `ins:me`, page by page.

API calls carry the web client's headers: `X-IG-App-ID` and `X-ASBD-ID` (the `APP_ID` and
`ASBD_ID` constants), `X-IG-WWW-Claim`, `X-Requested-With`, a `Referer`, and `X-CSRFToken` from
the `csrftoken` cookie. The HTTP client sends a desktop Chrome user agent.

How each category is read:

- **`posts`, `reels`, `tagged`, `reposts`**: open the tab's page (`/<user>/`, `/<user>/reels/`,
  `/<user>/tagged/`, `/<user>/reposts/`) and scroll. A grid item whose files are all at full
  size is saved as listed. Any other item is kept aside and read after the scroll ends, from the
  media API or else from the post's own page `/p/<code>/`. The tabs not yet read are opened in
  background tabs meanwhile, so they are loaded when their turn comes.
- **`stories`**: load the profile page for the user's id, then ask the story API; if it refuses,
  read the story page `/stories/<user>/`.
- **`highlights`**: the profile page lists them; each batch of 5 comes from `reels_media`, or,
  if it refuses, from each highlight's page `/stories/highlights/<id>/`.
- **`profile`**: the largest picture the profile page's results name.
- **`post` target**: the media API, then the post's page.

A video is saved from its DASH manifest when it has one (the best video and audio streams,
merged), else from its largest MP4. A photo is its largest image candidate.

**Pace and concurrency.** `pace` is 0.2: at least 0.2 seconds between requests to
`instagram.com`, page loads included. Up to 8 files of a target download at once (the default
`concurrency`). The CDN gets a `Referer: https://www.instagram.com/` header, which it requires.

## Where files go

A target's files go under `.flnd/media/ins/<target>/`, where `<target>` is the user name, or the
post's code for a `post` target. Inside it, the source declares:

- `folder = "{category}/{album}"`: the category's name, then the highlight's title for
  highlights (`album` is the source's own field). The `profile` category uses `profile`. A
  `post` target has no category, so its files sit directly in the target's folder.
- `filename`: the default `{date}_{id}{index}`, so a file is `<date>_<code>.<ext>`, or
  `<date>_<code>_<n>.<ext>` when the post has several files. A story is `<date>_<story id>`.
  Without a date, the name is just the rest (`<code>.jpg`).
- `key`: the default `{id}_{num}`, so the history remembers `ins:<code>_<n>`. The `stories`,
  `highlights` and `profile` categories set an empty key, so their history key is the file's
  path under the target's folder.

The profile picture keeps the file name the CDN gives it. A story with music also saves the
music as `<date>_<story id>_music.m4a`.

Example:

```
.flnd/media/ins/example_user/posts/2026-01-02_Cexample123_0.jpg
.flnd/media/ins/example_user/highlights/Travel/2026-01-02_1234567890.mp4
```

## Quirks

- **Rate limiting.** Instagram answers `{"message": "Please wait a few minutes before you try
  again.", "status": "fail"}` instead of a result (seen 2026-11, per the code comment). flnd
  waits and asks again, in the API and while scrolling.
- **flnd's own client refused.** Instagram may refuse the HTTP client (a 400, 401 or 403, or a
  login wall), likely by its TLS fingerprint. On the first such refusal flnd warns once and
  sends every later API call from inside the logged-in page instead. Handled since 2026-11-12.
- **Rotating claim.** Instagram sends a new `x-ig-set-www-claim` header, which must be echoed
  as `X-IG-WWW-Claim` on the next call. Handled since 2026-11-12.
- **Refused story and highlight APIs.** When the story or `reels_media` API refuses, flnd
  leaves it alone for 24 hours (remembered across runs) and reads story pages instead, saying so
  in one info line. Handled since 2026-11-13.
- **`reels_media` takes at most 5 reels a call**; more is refused. Handled since 2026-11-12.
- **Profile page no longer embeds the user or highlights** (2026-11, per the code comment). flnd
  also reads the page's own GraphQL calls, once the network is quiet.
- **Thumbnail-only grid items.** A grid item may carry only a small image. flnd treats an image
  as full size when it is at least 90% of the post's `original_width`; anything else is read
  again. Handled since 2026-11-12.
- **Dates from ids.** An Instagram id holds its creation time (milliseconds since
  2011-08-24 21:07:01.721 UTC, above its lowest 23 bits). flnd dates a not-yet-read grid item
  this way, so `--when` can skip it without a request; file names still use the post's own
  `taken_at`. Handled since 2026-11-12.
- **Codes and private posts.** A code is the post's id in base 64. A private post's longer code
  (over 11 characters) does not decode, and a decoded post whose code differs is never taken for
  it; both fall back to the post's page. Handled since 2026-11-12.
- **Tab result names.** Instagram has used two names for the reels and reposts tabs' result
  lists; flnd reads either. The reels tab wraps each post as `{"media": post}`, and reposts may
  come as `repost_grid_items`. Handled since 2026-11-11.
- **Pinned posts.** Up to 3 pinned posts sit above newer ones, so the "older than `--when`"
  stop waits for 5 in a row. Handled since 2026-11-14.
- **Story ids** carry their owner after an `_`; only the part before it is used. Handled since
  2026-11-11.
- **Image formats.** Older posts are served as WebP, newer as JPEG; the extension comes from the
  image URL (`jpg`, `webp`, `png`, `heic`). Handled since 2026-11-11.
- **Story music** comes in two shapes (a music sticker's asset, or the story's music metadata).
  Handled since 2026-11-13.
- **Login wall paths** are matched as whole path segments, so a profile named like
  `challengers` is not a wall. Handled since 2026-11-16.

## Limits

- **Story links are refused**: `instagram.com/stories/...` says "story links aren't supported;
  give the profile, and its stories come with it". Links under `instagram.com/explore`,
  `/accounts` and `/direct` are refused as "that's not a profile".
- **Expired stories** cannot be downloaded: only the stories active at run time are listed.
- **Private accounts and posts** are readable only when the login can see them.
- **Rate limits and login walls** are waited out (a login wall at most twice); a suspended or
  locked account stops the run.
- **`tagged` and `highlights`** are read in full each run; only the newest-first categories stop
  early.
- **`ins:me`** needs a login.
