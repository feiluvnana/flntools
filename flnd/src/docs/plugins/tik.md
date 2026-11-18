# TikTok (`tik`)

flnd downloads a TikTok profile's videos and photo posts, its stories, its playlists, its Reposts,
Liked and Favorites tabs, and its profile picture. It also downloads one video or photo post by
its id or link. A profile is read in a browser, which flnd drives itself; a single post is read
from TikTok's public embed page over plain HTTP, without a browser. No login is required: without
one, flnd warns and uses public access.

A *target* is what you give `flnd download`; a *category* is one part of a target, named after
its last `/`. See the [glossary](../glossary.md) and the [CLI guide](../cli.md).

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
whole; naming one for it is a usage error.

| Category | What it holds |
| --- | --- |
| `videos` | videos and photo posts that are in no playlist |
| `stories` | the profile's current stories |
| `playlists` | videos that are in a playlist, one folder per playlist |
| `reposts` | the Reposts tab |
| `liked` | the Liked tab, when the account made it public |
| `favorites` | the Favorites tab, when the account made it public |
| `profile` | the profile picture |

Without categories a profile gets its own categories, in the order above: every one except
`reposts`, `liked` and `favorites`. Those three are marked `own=False`, so they are taken only
when named (`tik:<user>/liked`), or with `/all`.

## Logging in

The plugin declares `login = cookies("sessionid", "sid_tt")`: `flnd login tik <how>` takes a
browser (read the cookies of an installed browser, such as `chrome`) or a cookie file's path (a
Netscape `cookies.txt` or JSON cookie file). Either cookie for `tiktok.com` proves a login.

Nothing in the plugin needs a login, and no login wall is known: a logged-out visitor is shown the
public profile. Without a login, flnd prints a warning and reads what is public. With one, the
saved cookies are loaded into the browser, and the CDN is sent the `tt_chain_token` and
`sessionid` cookies with every file, each only when it has a value. A login with only
`sid_tt` sends no `sessionid` at all; before 2026-11-16 it sent an empty `sessionid=`.

When a run starts, flnd warns if a login cookie has expired or expires within 24 hours. After a
run, cookies the site refreshed are written back into the saved login. `flnd login` shows which
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

1. flnd loads the profile page, with its feeds already listening so that no answer is lost, and
   waits 5 seconds for it to settle. Then it checks for a captcha.
2. It reads the profile's own data (its picture and its video count) from the
   `__UNIVERSAL_DATA_FOR_REHYDRATION__` script in the page.
3. `videos` and `playlists` share one scroll of the post list. Each item goes to `playlists` if
   it has a playlist name, else to `videos`. The scroll ends when a list response says
   `hasMore: false` (`page_state`, the feed's cursor reader).
4. `stories` has a feed of its own, listening from the page load, and is read with no scroll;
   flnd waits up to 10 seconds for the network to go quiet.
5. `reposts`, `liked` and `favorites` each click their tab (`data-e2e="repost-tab"`,
   `"liked-tab"`, `"favorites-tab"`) and scroll its list to the end. A tab the account doesn't
   show is logged as not public, and gives nothing.
6. `profile` takes the `avatarLarger` picture from the profile's data.

A list ends when a response says `hasMore` is false, or when the page stops loading more.
Those are the only stops the plugin makes itself; the engine makes the others (see *Early
stops* below).

**A single post** is read from `https://www.tiktok.com/embed/v2/<id>` over HTTP: its
`__NEXT_DATA__` script holds the post's data. A 404 or 410 from that page means the post is gone,
and is `NotFound`. Any other refusal, a 401 or 403 included, is a site error, since the embed
page is public. A page with no post in it is `Changed`.

When a saved file's link has expired (the CDN answers 403 or 410), flnd asks the post's embed
page once for a fresh link to the same photo or video. A profile picture has no embed page, so
it is not asked for this way.

**Which video file:** each video lists several encodings (`bitrateInfo`). flnd takes the one with
the most pixels, bitrate breaking a tie, and skips DASH streams. With no usable entry, it takes
`playAddr`. Every CDN address a file has is kept and tried in order.

**Pacing:** `pace` is 0.2, so requests to TikTok's own domains are at least 0.2 seconds apart; no
limit is known. The plugin sets no `limits`: each CDN host's files download 8 at once, adapting up
to 32. Requests are sent with a fixed desktop Chrome user agent.

## Where files go

The plugin declares:

- `folder = "{part}/{album}"`: `part` is `videos` or `images` for the `videos` category (by
  whether the post is a video or photos), `albums` for a playlist video, and otherwise the
  category's name. `album` is the playlist's name, and empty outside `albums`.
- `fields = ("part", "album", "idx")`: the post fields the templates use.
- `filename = "{date}_{post}_{idx}"`: `post` is the post's id, and `idx` is empty for a video, so
  the `_` before it is dropped, and the photo's place (from 1) for a photo post. The date is the
  UTC day the post was made.
- `key = "{post}_{idx}"`: the history key, stored as `tik:<id>` for a video and `tik:<id>_<n>` for
  the n-th photo. These are the names and keys files have always had: changing either would
  download every saved file again.
- The `profile` category has its own `folder = "profile"`, and its key is the file's path,
  `profile/avatar_<picture id>.jpg`. The picture is named `avatar_<picture id>.jpg`.

Files go under `.flnd/media/tik/<target>/`, where `<target>` is the user name or the post id:

```
.flnd/media/tik/example_creator/videos/2026-01-02_1234567890123456789.mp4
.flnd/media/tik/example_creator/images/2026-01-02_1234567890123456780_1.jpg
.flnd/media/tik/example_creator/albums/Example Playlist/2026-01-03_1234567890123456781.mp4
.flnd/media/tik/example_creator/profile/avatar_1234567890.jpg
```

## Quirks

- **Captchas.** TikTok shows a slider or puzzle captcha a few seconds after the page looks loaded.
  flnd finds it by its container or its words, and gives you 60 seconds to solve it in the browser
  window. If it is still there, flnd holds the target as a block (the engine's holds run 30 s,
  then 120 s, then 480 s), then opens the profile again and waits for the captcha once more. A
  fourth unsolved captcha fails the target; the count starts again after ten minutes with no
  block. Captcha detection handled since 2026-11-11. A captcha can also come over the grid while
  it scrolls: when the post list then gave nothing, flnd watches again, waits the captcha out and
  reads the grid again (handled since 2026-11-11).
- **Pinned videos** sit at the top of the post list, out of date order. `videos` and `playlists`
  are `newest_first`, so the engine's early stops apply (below); they wait for 5 posts in a row,
  so a few old pinned posts don't end the list.
- **Photo posts** come as one item per photo, all with the post's id, and the plugin yields one
  post per photo: a photo's key counts from 1 even in a one-photo post, which no engine field
  gives. The engine's streaks count posts, so each photo counts once: a pinned carousel of 5 or
  more photos, older than `--when` or already saved, is enough to end the scroll. (Before
  2026-11-16 the source counted a carousel once, with stops of its own.)
- **Walled post list.** When the profile page loads but its post list gives nothing, on a profile
  that lists videos, `videos` reads every post linked on the page from its embed page instead.
  A post that fails there is logged and skipped. Then the category fails with `Hidden`, with the
  hint to log in with another account or try again later. Handled since 2026-11-11.
- **Walled playlists.** `playlists` can't tell the posts apart in that read, so it fails with
  `Hidden` too. Its hint says to log in with another account, or to download the `videos` category
  as well, since that reads the linked posts, playlists' included.
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

## Early stops

The engine owns them; the plugin only yields the posts in the order the list gives them.

- With `--when`, a `newest_first` listing (`videos`, `playlists`) stops after 5 posts in a row
  older than its start, which closes the scroll.
- A sync (`flnd sync` of a followed profile) stops a `newest_first` listing once it has caught up:
  5 saved posts in a row, or the newest post an earlier run saw and the one after it.
- `stories`, `reposts`, `liked` and `favorites` are not `newest_first` (a repost is ordered by
  when it was reposted), so they are always read to their end.

## Limits

- The plugin declares no `unsupported` links; any other TikTok link is refused with the list of
  forms above.
- Liked and Favorites are read only when the account made them public. A tab that is missing is
  reported as "not public on this account", and an empty one as "nothing public".
- A tab that is there but doesn't take the click within 10 seconds fails its category with
  `Changed`.
- Stories are the ones the page shows on load; older stories are not listed.
- A profile always needs the browser, and a captcha may need you at the window. A fourth unsolved
  captcha fails the target.
- A walled post list fails `videos` and `playlists` with `Hidden`, so another account may be
  tried (see Quirks).
- No rate limit is known. TikTok's answer to heavy use is the captcha.
