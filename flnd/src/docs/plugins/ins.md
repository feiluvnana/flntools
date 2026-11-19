# Instagram (`ins`)

flnd downloads a profile's posts, reels, tagged posts, reposts, active stories, highlights and
profile picture from Instagram, or one post or reel by its link. It can also list every account
the logged-in user follows. It reads the pages in a browser: profile tabs are read by scrolling a
real page, and a lazy post's files come from its own page. Only the list of followed accounts
calls Instagram's API. The login is optional, but Instagram shows a logged-out session little,
and the `ins:me` target needs one. The plugin is `src/plugins/ins.py`, on plugin API 2.

## Targets

A *target* is what you ask flnd to download (see the [glossary](../glossary.md)). Each form
below is read by one *route*, a method of the plugin named after the kind of target it reads.

| You type | Route | What it downloads |
| --- | --- | --- |
| `ins:example_user` | `user` | The profile's categories (below) |
| `https://www.instagram.com/example_user/` | `user` | The same; so is `/reels/` after it |
| `https://www.instagram.com/p/<code>/` | `post` | One post, every file of it |
| `https://www.instagram.com/reel/<code>/` | `post` | One reel; `/reels/` and `/tv/` too |
| `https://www.instagram.com/example_user/p/<code>/` | `post` | One post, under a profile |
| `ins:me` | `me` | Every account the login follows, each as its own `user` target |

A post's *code* is the short text in its link. The `post` route reads `instagram.com/p/<code>`
as a post, never as a user named `p`.

## Categories

A *category* is one part of a profile that can be downloaded on its own. Without a category
named, a profile gets its own content: every category except `tagged` and `reposts`, which are
other people's posts and are taken only when named, or with `/all`. `ins:<user>/reels` names
one. A `post` target takes no category: it is always downloaded whole. For `ins:me`, the
categories it names (`ins:me/reels`) apply to each followed account it finds.

| Category | What it holds |
| --- | --- |
| `posts` | The posts grid, newest first, to its end |
| `reels` | The reels tab, newest first |
| `tagged` | Posts the profile is tagged in (other people's posts, so only when named) |
| `reposts` | Posts the profile reposted, newest first (other people's posts, so only when named) |
| `stories` | The stories active now |
| `highlights` | Every highlight, each in its own folder |
| `profile` | The profile picture, at its HD size |

`posts`, `reels` and `reposts` are *newest first*. The engine's early stops apply to them. A
download stops after 5 posts in a row older than the start of `--when`. A sync stops once it
has met the head it saw on an earlier run, then two saved posts in a row. A pinned
post, which Instagram lists above newer ones, does not count toward the older stop. The plugin
scrolls a tab until its `page_info` says there is no next page. `tagged`, `stories`, `highlights`
and `profile` are read in full on every run.

## Logging in

`flnd auth login ins <method>` logs in. The methods are `browser`, `chrome`, `file <path>`,
`firefox` and `safari`; they are the cookie methods the plugin declares (`cookies("sessionid")`
in `ins.py`). Left out in a terminal, flnd asks for the method from a menu, and asks for the path
when the method is `file`.

- **`browser`**: opens a window of flnd's own Chromium at Instagram's login page. Log in there,
  and flnd reads the cookies once `sessionid` is set. Close the window to give up.
- **`chrome`**: reads the cookies from your Chrome through flnd's Chrome extension (see
  `flnd extension install`).
- **`file <path>`**: reads a Netscape `cookies.txt` or a JSON export of cookies.
- **`firefox`** and **`safari`**: read the cookies from that browser. Safari is read on macOS
  only.

The login is accepted only when the cookies hold `sessionid`. Only the cookies for
`instagram.com` are kept. The login is sealed in the vault, `metadata/secret.db` in the home
folder. The key that opens it is kept in the user's key file, outside the home. Each run loads
the cookies into its HTTP client and its browser, and at the end writes back any of them the site
refreshed.

Without a login, flnd still tries: it opens the same pages, and gets what Instagram shows a
logged-out visitor. `ins:me` needs the logged-in account's id. It is read from the `ds_user_id`
cookie, or else from the number at the start of the `sessionid` cookie. Without either, the
command stops with a login error, "ins: not logged in, so there is no account to read", and the
hint `run flnd auth login ins`.

At the start of a run, flnd warns if `sessionid` has expired, or expires within 3 days. When the
session stops working mid-run, a page or a request that lands on one of the plugin's `walls` is
waited out. `/accounts/login` and `/challenge` (a *login wall*) and `/accounts/suspended` each
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
   `page_info` says there is no next page.
3. **Requests from the page.** `ins:me` reads `friendships/<id>/following/` page by page, from
   inside the logged-in tab, the way the page's own script would ask. A lazy post's page is
   fetched the same way (see below).

Requests to `/api/` and `/graphql` carry the web client's headers: `X-IG-App-ID` and `X-ASBD-ID`
(the `APP_ID` and `ASBD_ID` constants), `X-IG-WWW-Claim`, `X-Requested-With`, a `Referer`, and
`X-CSRFToken` from the `csrftoken` cookie. A page's own request is left as it is.

How each category is read:

- **`posts`, `reels`, `tagged`, `reposts`**: open the tab's page (`/<user>/`, `/<user>/reels/`,
  `/<user>/tagged/`, `/<user>/reposts/`) and scroll it. A grid item that is the whole post is
  saved as listed: it has a date, and its files are at full size. Any other item is listed as a
  *lazy* post. It is dated by its id, and its files are read only when the engine wants them,
  from its page. That page is fetched from inside the logged-in tab
  (`ctx.fetch(..., in_page=True)`), so the fetch never navigates the scrolling tab. When the
  fetched page holds no such post, the post's own page is opened with a feed that doesn't
  scroll, and its results are read instead. Whether the fetched page embeds the post has not been
  confirmed on a live page. When it does not, the second step reads it.
- **`stories`**: open `/stories/<user>/` and read its page: each story is a post with its media.
  A page that fails to load (an HTTP error) raises an error.
- **`highlights`**: the profile page's results list them. Each highlight's page
  (`/stories/highlights/<id>/`) is opened and read, in a folder named by its title. A page that
  fails to load raises an error.
- **`profile`**: the HD profile picture the profile's results name.
- **`post` target**: the post's page is opened with no scroll, and its results are read. The
  post must have the code the link names. A page with no such post is `NotFound`, and so is a
  post with no files.

A video is saved from its DASH manifest when it has one (the best video and audio streams,
merged), else from its largest MP4. When a post has both, the MP4 is kept after the DASH pair as
a mirror: it is saved alone, without the audio, if the DASH video fails. A DASH pair is saved as
`.webm` only when its video stream is WebM, else as `.mp4`. A photo is its largest image
candidate.

**Pace and concurrency.** The plugin sets `pace = 2.0`, so 2 seconds apply between two page
requests of one account to stay below Instagram's rate limit. Each CDN host's files download 8
at once, adapting up to 32 (the plugin sets no `limits` of its own). The CDN gets a
`Referer: https://www.instagram.com/` header, which it requires.

## Where files go

A target's files go under `.flnd/media/ins/<target>/`, where `<target>` is the user name, or the
post's code for a `post` target. Inside it, the plugin declares:

- `folder` is the category's name by default. `highlights` uses `highlights/{album}`, so each
  highlight goes in a folder named by its title, and `profile` uses `profile`. A `post` target
  has no category, so its files sit directly in the target's folder.
- `filename = "{date}_{post}{index}"`, so a file is `<date>_<code>.<ext>`, or
  `<date>_<code>_<n>.<ext>` when the post has several files. A story is `<date>_<story id>`.
  Without a date, the name is just the rest (`<code>.jpg`). A lazy post's files, and a file
  whose URL has another extension, are named by the plugin the same way, from the post's own
  `taken_at`.
- `key` is the default `{post}_{num}`, so the history remembers `ins:<code>_<n>`. The `stories`,
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
  again.", "status": "fail"}` instead of a result (seen 2026-11). The plugin's `blocked()` reads
  that answer as a block, and the page and feed loops hold on it too. The engine's block rules
  then hold the requests: 30 seconds, then 120, then 480.
- **Rotating claim.** Instagram sends a new `x-ig-set-www-claim` header, which must be echoed as
  `X-IG-WWW-Claim` on the next call. The plugin keeps the last one for its API calls.
- **Stories and highlights come from their pages.** They are read from the story and highlight
  pages, not from an API.
- **The profile page** no longer embeds the user or the highlights (seen 2026-11). The plugin
  reads the page's own GraphQL calls too, which bring them.
- **Thumbnail-only grid items.** A grid item may carry only a small image. An image counts as
  full size when it is at least 90% of the post's `original_width`. Anything else is listed as a
  lazy post and read from its page.
- **Dates from ids.** An Instagram id holds its creation time: milliseconds since
  2011-08-24 21:07:01.721 UTC, above its lowest 23 bits. flnd dates a grid item this way, so
  `--when` can skip it without a request. File names still use the post's own `taken_at`.
- **Post codes.** A grid item's code is its `code`, or its `pk` when it has no code. A post page
  is taken for the link's post only when its code is the link's. A page with another code is
  not it.
- **Tab result names.** Instagram has used two names for the reels and reposts tabs' result
  lists, and flnd reads either. The reels tab wraps each post as `{"media": post}`, and reposts
  may come as `repost_grid_items`.
- **Pinned posts** sit above newer ones. They are marked as pinned, so they do not count toward
  the stop for older posts.
- **Story ids** carry their owner after an `_`. Only the part before it is used.
- **Image formats.** Older posts are served as WebP, newer ones as JPEG. The extension comes from
  the image URL (`jpg`, `webp`, `png` or `heic`), and `jpg` is used for any other.
- **Story music** comes in two shapes: a music sticker's asset, or the story's music metadata.
  Both are read.
- **Throttled sessions** are sent to the login wall or a challenge page, which lifts after a
  wait, so a wall is waited out before it counts as a lost login.
- **Login wall paths** are matched as whole path segments, so a profile named like `challengers`
  is not a wall.

## Limits

- **Story links are refused**: `instagram.com/stories/...` says "story links aren't supported;
  give the profile, and its stories come with it". Links under `instagram.com/explore`,
  `/accounts` and `/direct` are refused as "that's not a profile".
- **Expired stories** cannot be downloaded: only the stories active at run time are listed.
- **Private accounts and posts** are readable only when the login can see them. The plugin does
  not tell a private post from a missing one: a post page with no such post is `NotFound`, never
  `Hidden`.
- **Rate limits and login walls** are waited out as described above. A suspended or locked
  account ends in an expired login.
- **`tagged`, `stories`, `highlights` and `profile`** are read in full on every run. Only the
  newest-first categories (`posts`, `reels`, `reposts`) stop early.
- **`ins:me`** needs a login.

## Testing

Unit tests fake the network, the browser and the clock. Live checks are done by hand, on a test
account, and never in unit tests. A change to this plugin must keep the routes and refusals
above, the `newest_first` order of each category, the wall and block waits, and the history keys
(`ins:<code>_<n>` for posts, `{path}` for stories, highlights and the profile picture), so that
files already saved are not saved again.
