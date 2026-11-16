# TikTok (`tik`)

flnd downloads a TikTok profile's videos and photo posts, its stories, its playlists, its Reposts,
Liked and Favorites tabs, and its profile picture. It also downloads one video or photo post by
its id or link. A profile is read in a browser, which flnd drives itself; a single post is read
from TikTok's public embed page over plain HTTP, without a browser. No login is required: without
one, flnd warns and uses public access.

A *target* is what you give `flnd download`; a *category* is one part of a target, chosen with
`--from`. See the [glossary](../glossary.md) and the [CLI guide](../cli.md).

## Targets

| You type | Route | What it downloads |
| --- | --- | --- |
| `tik:example_creator` or `tik:@example_creator` | `user` | the profile, by category |
| `https://www.tiktok.com/@example_creator` | `user` | the same |
| `tik:1234567890123456789` (16 digits or more) | `video` | one video or photo post |
| `https://www.tiktok.com/@example_creator/video/1234567890123456789` | `video` | the same |
| `https://www.tiktok.com/@example_creator/photo/1234567890123456789` | `video` | the same |
| `https://www.tiktok.com/video/1234567890123456789` | `video` | the same |
| `https://www.tiktok.com/photo/1234567890123456789` | `video` | the same |

The `video` route is tried before `user`, so a bare number of 16 digits or more is always a post
id, never a user name. A short link on a subdomain of `tiktok.com` (such as `vm.tiktok.com`)
matches no route; the engine follows its redirect once and reads the link it lands on.

## Categories

These belong to the `user` route. A single post (`video`) has no categories and is downloaded
whole; `--from` with one is a usage error.

| Category | What it holds |
| --- | --- |
| `videos` | videos and photo posts that are in no playlist |
| `stories` | the profile's current stories |
| `playlists` | videos that are in a playlist, one folder per playlist |
| `reposts` | the Reposts tab |
| `liked` | the Liked tab, when the account made it public |
| `favorites` | the Favorites tab, when the account made it public |
| `profile` | the profile picture |

There is no default category. For a profile, `--from` is required: name one or more categories,
or `all` for every one, in the order above.

## Logging in

`flnd auth login` offers two methods: `browser` (read the cookies of an installed browser) and
`file` (a Netscape `cookies.txt` or JSON cookie file). A login is proven by a `sessionid` or
`sid_tt` cookie for `tiktok.com`.

Nothing in the source needs a login. Without one, flnd prints a warning and reads what is public.
With one, the saved cookies are loaded into the browser, and the `sessionid` (with
`tt_chain_token` when the jar has one) is sent to TikTok's CDN with every file.

When a run starts, flnd warns if a login cookie has expired or expires within 24 hours. After a
run, cookies the site refreshed are written back into the saved login. `flnd auth ls` shows which
login cookies are about to expire. An expired login is not detected otherwise: TikTok then serves
what a logged-out visitor sees.

## How it fetches

**A profile** is read in the browser. flnd does not call TikTok's API itself: it opens
`https://www.tiktok.com/@<user>` and watches the JSON responses the page's own scripts fetch, while
scrolling the page to its end. Each category listens for its own API paths:

| Category | API path the page calls |
| --- | --- |
| `videos`, `playlists` | `/api/post/item_list` |
| `stories` | `/api/story/item_list`, `/api/user/story` |
| `reposts` | `/api/repost/item_list` |
| `liked` | `/api/favorite/item_list` |
| `favorites` | `/api/user/collect/item_list` |

1. flnd loads the profile page and waits 5 seconds for it to settle, then checks for a captcha.
2. It reads the profile's own data (its picture and its video count) from the
   `__UNIVERSAL_DATA_FOR_REHYDRATION__` script in the page.
3. `videos` and `playlists` share one scroll of the post list. Each item goes to `playlists` if
   it has a playlist name, else to `videos`.
4. `stories` arrive with the page load, without scrolling; flnd waits up to 10 seconds for the
   network to go quiet.
5. `reposts`, `liked` and `favorites` each click their tab (`data-e2e="repost-tab"`,
   `"liked-tab"`, `"favorites-tab"`) and scroll its list to the end.
6. `profile` takes the `avatarLarger` picture from the profile's data.

A list ends when a response says `hasMore` is false, or when the page stops loading more.

**A single post** is read from `https://www.tiktok.com/embed/v2/<id>` over HTTP: its
`__NEXT_DATA__` script holds the post's data. A failed fetch is a warning for that post only.

**Which video file:** each video lists several encodings (`bitrateInfo`). flnd takes the one with
the most pixels, bitrate breaking a tie, and skips DASH streams. With no usable entry, it takes
`playAddr`. Every CDN address a file has is kept and tried in order.

**Pacing:** `pace` is 0.2, so requests to TikTok's own domains are at least 0.2 seconds apart (a
code comment says no limit is known). `concurrency` is 8: up to 8 files of one target download at
once. Requests are sent with a fixed desktop Chrome user agent.

## Where files go

The class declares:

- `folder = "{part}/{album}"`: `part` is `videos` or `images` for the `videos` category (by
  whether the post is a video or photos), `albums` for a playlist video, and otherwise the
  category's name. `album` is the playlist's name, and empty outside `albums`.
- `filename = "{date}_{id}_{idx}"`: `idx` is empty for a video, so the `_` before it is dropped,
  and the photo's place (from 1) for a photo post. The date is the UTC day the post was made.
- `key = "{id}_{idx}"`: the history key, stored as `tik:<id>` for a video and `tik:<id>_<n>` for
  the n-th photo.
- The `profile` category has its own `folder = "profile"` and an empty key, so its history key is
  the file's path. The picture is named `avatar_<picture id>.jpg`.

Files go under `.flnd/media/tik/<target>/`, where `<target>` is the user name or the post id:

```
.flnd/media/tik/example_creator/videos/2026-01-02_1234567890123456789.mp4
.flnd/media/tik/example_creator/images/2026-01-02_1234567890123456780_1.jpg
.flnd/media/tik/example_creator/albums/Example Playlist/2026-01-03_1234567890123456781.mp4
.flnd/media/tik/example_creator/profile/avatar_1234567890.jpg
```

## Quirks

- **Captchas.** TikTok shows a slider or puzzle captcha a few seconds after the page looks loaded.
  flnd finds it by its container or its words, gives you 60 seconds to solve it in the browser
  window, then treats it as a rate limit: it backs off, reloads and checks again. A fourth
  unsolved captcha fails the target. Captcha detection handled since 2026-11-11; the back-off
  and reload since 2026-11-12.
- **Pinned videos** sit at the top of the post list, out of date order. `videos` stops scrolling
  only after 5 posts in a row older than `--time`'s start, or 5 already-saved posts in a row, so
  a few old pinned posts don't end the list. Handled since 2026-11-14.
- **Photo posts** come as one item per photo, all with the post's id. Each post counts once toward
  those streaks. Handled since 2026-11-16.
- **Walled post list.** When the profile page loads but its post list gives nothing, `videos`
  reads every post linked on the page from its embed page instead. Handled since 2026-11-11.
- **Dates.** A post's time is its `createTime`. When that is missing or a placeholder, it is read
  from the id, whose upper bits are the Unix time in seconds. Handled since 2026-11-11.
- **CDN 403s.** TikTok's CDN refuses a file without a `Referer`, the user agent and the session
  cookies (handled since 2026-11-11), and some posts are refused on one CDN host but not another,
  so every address is tried (handled since 2026-11-13).
- **DASH entries.** A DASH stream saved as an MP4 would have no sound, so entries marked `dash`
  (in `Format`, `GearName` or `PlayAddr.UrlKey`) or with a `.mpd` URL are skipped. The code
  comment dates this 2026-11 and says the marks are unconfirmed until a live check.
- **Names.** TikTok's Liked tab is served from `/api/favorite/item_list`, and its Favorites tab
  from `/api/user/collect/item_list`. Handled since 2026-11-11.
- **Double `@`.** A link like `tiktok.com/@@example_creator` is refused, not read as a user
  name. Handled since 2026-11-12.

## Limits

- The source declares no `unsupported` links; any other TikTok link is refused with the list of
  forms above.
- Liked and Favorites are read only when the account made them public. A tab that is missing is
  reported as "not public on this account", and an empty one as "nothing public".
- A tab that doesn't open within 10 seconds is skipped with a warning.
- Stories are the ones the page shows on load; older stories are not listed.
- A profile always needs the browser, and a captcha may need you at the window. A captcha still
  there after 3 back-offs fails the target.
- No rate limit is known. TikTok's answer to heavy use is the captcha.
