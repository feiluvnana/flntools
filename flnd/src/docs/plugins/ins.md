# Instagram (`ins`)

flnd downloads a profile's posts, reels, tagged posts, reposts, active stories, highlights and
profile picture from Instagram, or one post or reel by its link. It can also list every account
the logged-in user follows. It reads from the logged-in browser: profile tabs are read by
scrolling a real page, and a lazy post's files come from its own page. No API call is made. A
login is not required by the code, but in practice Instagram shows little to a logged-out
session, and the `ins:me` target cannot work without one. The plugin is `src/plugins/ins.py`, on
plugin API 2.

## Targets

A *target* is what you ask flnd to download (see the [glossary](../glossary.md)). Each form
below is read by one *route*, a method of the plugin named after the kind of target it reads.

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
categories ([the CLI guide](../cli.md)) a profile gets its own content: every category except
`tagged` and `reposts`; `ins:<user>/reels` names some, and `/all` takes all of them. A `post`
target takes none: it is always downloaded whole. For `ins:me`, the categories it names
(`ins:me/reels`) apply to each followed account it finds.

| Category | What it holds |
| --- | --- |
| `posts` | The posts grid, newest first, to its end |
| `reels` | The reels tab, newest first |
| `tagged` | Posts the profile is tagged in (other people's posts, so only when named) |
| `reposts` | Posts the profile reposted, newest first (other people's posts, so only when named) |
| `stories` | The stories active now |
| `highlights` | Every highlight, each in its own folder |
| `profile` | The profile picture, at its largest size |

`posts`, `reels` and `reposts` are *newest first*. The early stops are the engine's, not the
plugin's: a sync of one of them stops after 5 posts in a row that are already saved, and a
download stops after 5 in a row older than the start of `--when`. Either closes the tab's scroll
at once. The plugin ends a tab's scroll where Instagram's own `page_info` says there is no next
page. `tagged`, `stories`, `highlights` and `profile` are read in full each run.

A followed target (see `ins:me`) takes `tagged` and `reposts` only if its stored categories name
them.

## Logging in

`flnd auth login ins <how>` takes a browser or a cookie file's path. Left out in a terminal,
the way is asked with numbered choices:

- **From your browser**: reads Instagram's cookies from Chrome, Firefox, Edge, Brave,
  Chromium, or Safari on macOS.
- **From a cookie file**: reads a Netscape `cookies.txt` or a JSON export.

The plugin declares `login = cookies("sessionid")`, not required: the login is accepted only
when the cookies hold `sessionid`. They are stored in the home's vault,
`metadata/secret.db`. Each run loads them into its HTTP client and its browser, and at the
end writes back any of those cookies the site refreshed.

Without a login, flnd still tries: it opens the same pages, and gets what Instagram shows a
logged-out visitor. `ins:me` needs the logged-in account's id, read from the `ds_user_id` cookie,
or else from the number at the start of the `sessionid` cookie. Without either it stops with a
login error, "ins: not logged in, so there is no account to read", and the hint
`run flnd auth login ins`.

When a run starts, flnd warns if `sessionid` has expired or will expire within a day. When the
session stops working mid-run, a page or a request that lands on one of the plugin's `walls` is
waited out: `/accounts/login` and `/challenge` (a *login wall*) and `/accounts/suspended` each
get a wait of 30 seconds, then 120 seconds, and the page is loaded again. After that the job
moves to another account, or fails with a login error and the account is marked expired.
Walls are matched as whole path segments.

## How it fetches

The plugin reads three ways, all from the logged-in browser:

1. **Embedded results.** An Instagram page carries GraphQL results inside
   `<script type="application/json">` blocks (the ones holding `__bbox`). flnd reads these
   first when a page loads.
2. **Captured responses.** While a profile tab scrolls, flnd keeps the JSON of every response
   whose URL contains `graphql`, and reads the posts from them. It scrolls until the tab's
   `page_info` says there is no next page (the tab's cursor reader). A feed that gives no cursor
   also ends its scroll when its bottom stays quiet.
3. **Requests from the page.** `ins:me` reads `friendships/<id>/following/` page by page, from
   inside the logged-in tab, the way the page's own script would ask. A lazy post's page is
   fetched the same way (see below).

Requests to `/api/` and `/graphql` carry the web client's headers: `X-IG-App-ID` and `X-ASBD-ID`
(the `APP_ID` and `ASBD_ID` constants), `X-IG-WWW-Claim`, `X-Requested-With`, a `Referer`, and
`X-CSRFToken` from the `csrftoken` cookie. A page's own request is left as it is.

How each category is read:

- **`posts`, `reels`, `tagged`, `reposts`**: open the tab's page (`/<user>/`, `/<user>/reels/`,
  `/<user>/tagged/`, `/<user>/reposts/`) and scroll it. A grid item whose files are all at full
  size is saved as listed. Any other item is listed as a *lazy* post: it is dated by its id, and
  its files are read only when the engine wants them, from its page. That page is fetched from
  inside the logged-in tab (`ctx.fetch(..., in_page=True)`), and the post in it is read. The
  fetch never navigates the scrolling tab. When the fetched page holds no such post, the post's
  own page is opened with a feed that doesn't scroll, and its results are read instead. Whether
  the fetched page embeds the post is still to be confirmed by a live check; until then the second
  step is the fallback that covers it.
- **`stories`**: open `/stories/<user>/` and read its document: each story is a post with its
  media. A page that fails to load raises an error.
- **`highlights`**: the profile page lists them; each highlight's page
  (`/stories/highlights/<id>/`) is opened and its document read, in a folder named by its title.
  A page that fails to load raises an error.
- **`profile`**: the largest picture the profile page's results name.
- **`post` target**: the post's page is opened with no scroll, and its results are read. The
  post must have the code the link names; a page with no such post is `NotFound`, and so is a
  post with no files. No API call is made.

A video is saved from its DASH manifest when it has one (the best video and audio streams,
merged), else from its largest MP4. When a post has both, the MP4 is kept after the DASH pair as
a mirror: it is saved alone, without the audio, if the DASH video fails. A DASH pair is saved as
`.webm` only when its video stream is WebM, else as `.mp4`. A photo is its largest image
candidate.

**Pace and concurrency.** The plugin sets no `pace`, so the class default of 0.5 seconds applies
between requests to `instagram.com`, page loads included. Its requests share the one bucket of
that host. Each CDN host's files download 8 at once, adapting up to 32 (no `limits` of its own).
The CDN gets a `Referer: https://www.instagram.com/` header, which it requires.

## Where files go

A target's files go under `.flnd/media/ins/<target>/`, where `<target>` is the user name, or the
post's code for a `post` target. Inside it, the plugin declares:

- `folder = "{category}"` by default: the category's name. `highlights` uses
  `highlights/{album}`, so each highlight goes in a folder named by its title, and `profile` uses
  `profile`. A `post` target has no category, so its files sit directly in the target's folder.
- `filename = "{date}_{post}{index}"`, so a file is `<date>_<code>.<ext>`, or
  `<date>_<code>_<n>.<ext>` when the post has several files. A story is `<date>_<story id>`.
  Without a date, the name is just the rest (`<code>.jpg`). A lazy post's files, and a file
  whose URL has another extension, are named by the plugin the same way, from the post's own
  `taken_at`.
- `key`: the default `{post}_{num}`, so the history remembers `ins:<code>_<n>`. The `stories`,
  `highlights` and `profile` categories set `key = "{path}"`, so their history key is the file's
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
  again.", "status": "fail"}` instead of a result (seen 2026-11, per the code comment). The
  plugin's `blocked()` reads that answer as a block, and the feed loop holds on it too; the
  engine's block rules then hold the requests (30 seconds, then 120, then 480).
- **Rotating claim.** Instagram sends a new `x-ig-set-www-claim` header, which must be echoed
  as `X-IG-WWW-Claim` on the next call. The plugin keeps the last one for the `ins:me` calls.
  Handled since 2026-11-12.
- **Story and highlight pages.** Because Instagram's story and reels REST APIs fail with
  HTTP 500, stories and highlights are read directly from their pages (2026-11).
- **Profile page no longer embeds the user or highlights** (2026-11, per the code comment). The
  plugin also reads the page's own GraphQL calls.
- **Thumbnail-only grid items.** A grid item may carry only a small image. flnd treats an image
  as full size when it is at least 90% of the post's `original_width`; anything else is listed as
  a lazy post and read from its page. Handled since 2026-11-12.
- **Dates from ids.** An Instagram id holds its creation time (milliseconds since
  2011-08-24 21:07:01.721 UTC, above its lowest 23 bits). flnd dates a not-yet-read grid item
  this way, so `--when` can skip it without a request; file names still use the post's own
  `taken_at`. Handled since 2026-11-12.
- **Post codes.** A grid item's code is its `code`, or its `pk` when it has no code. A post page
  is taken for the link's post only when its code is the link's; a page with another code is not
  it. Handled since 2026-11-12.
- **Tab result names.** Instagram has used two names for the reels and reposts tabs' result
  lists; flnd reads either. The reels tab wraps each post as `{"media": post}`, and reposts may
  come as `repost_grid_items`. Handled since 2026-11-11.
- **Pinned posts.** Up to 3 pinned posts sit above newer ones, so the engine's "older than
  `--when`" stop waits for 5 in a row. Handled since 2026-11-14.
- **Story ids** carry their owner after an `_`; only the part before it is used. Handled since
  2026-11-11.
- **Image formats.** Older posts are served as WebP, newer as JPEG; the extension comes from the
  image URL (`jpg`, `webp`, `png`, `heic`). Handled since 2026-11-11.
- **Story music** comes in two shapes (a music sticker's asset, or the story's music metadata).
  Handled since 2026-11-13.
- **Throttled sessions** are sent to the login wall or a challenge page, which lifts after a
  wait, so a wall is waited out before it counts as a lost login (2026-11).
- **Login wall paths** are matched as whole path segments, so a profile named like
  `challengers` is not a wall. Handled since 2026-11-16.

## Limits

- **Story links are refused**: `instagram.com/stories/...` says "story links aren't supported;
  give the profile, and its stories come with it". Links under `instagram.com/explore`,
  `/accounts` and `/direct` are refused as "that's not a profile".
- **Expired stories** cannot be downloaded: only the stories active at run time are listed.
- **Private accounts and posts** are readable only when the login can see them. The marker of a
  private account the login doesn't follow is not yet known, so a post page with no post is
  `NotFound`, not `Hidden`.
- **Rate limits and login walls** are waited out as described above; a suspended or locked
  account ends in an expired login.
- **`tagged`, `stories`, `highlights` and `profile`** are read in full each run; only the
  newest-first categories (`posts`, `reels`, `reposts`) stop early.
- **`ins:me`** needs a login.
