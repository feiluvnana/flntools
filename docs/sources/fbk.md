# Facebook (`fbk`)

flnd downloads the photos and videos of a Facebook profile or page: its posts, photos, tagged
photos, albums, reels, videos, stories, and its profile and cover pictures. It reads everything
in a browser, by loading the profile's pages and scrolling them. Single posts, photos and videos
are not supported: give the profile. A login is not required by the code, but Facebook shows
little to a logged-out browser. The source is `flnd/sources/fbk.py`.

## Targets

A *target* is what you ask flnd to download (see the [glossary](../glossary.md)). Every form
below is read by the one *route* this source has, `user`.

| You type | Route | What it downloads |
| --- | --- | --- |
| `fbk:example.page` | `user` | The profile's categories (below) |
| `https://www.facebook.com/example.page` | `user` | The same; a tab link such as `/photos` too |
| `https://www.facebook.com/@example.page` | `user` | The same |
| `https://www.facebook.com/profile.php?id=1234567890` | `user` | The profile with that id |
| `https://www.facebook.com/people/Example-Name/1234567890` | `user` | The profile with that id |
| `fbk:1234567890` | `user` | The profile with that id |

Every `facebook.com` form also works on `fb.com`. The `profile.php` and `people` forms come
before the name form, so neither is read as a user name.

## Categories

A *category* is one part of a profile that can be downloaded on its own. Without
[`--only`](../cli.md) a profile gets every category; `--only` names some of them.

| Category | What it holds |
| --- | --- |
| `posts` | Photos and videos of the profile's own posts (the profile page itself) |
| `photos` | The photos tab |
| `tagged` | Photos the profile is tagged in |
| `albums` | Every album on the albums tab, each in its own folder |
| `reels` | The reels tab |
| `videos` | The videos tab |
| `stories` | The stories active now |
| `profile` | The profile picture and the cover photo |

No category is newest first: each tab is scrolled to its end every run.

Media another account owns is left out of a tab, when the profile header gives the profile's id.
A video found in the `posts`, `photos` or `tagged` tab is filed under `videos/`, and a photo that
names its album under `albums/<album>/`, whichever tab found it.

## Logging in

`flnd login fbk <how>` takes a browser or a cookie file's path. Left out in a terminal,
the way is asked with numbered choices:

- **From your browser**: reads Facebook's cookies from Chrome, Firefox, Edge, Brave or
  Chromium.
- **From a cookie file**: reads a Netscape `cookies.txt` or a JSON export.

The login is accepted when the cookies hold `c_user` or `xs`. They are stored encrypted in
`.flnd/metadata/secrets.db`, loaded into the browser each run, and any the site refreshed are
written back at the end.

Without a login, flnd opens the same pages and gets what Facebook shows a logged-out visitor.

When a run starts, flnd warns if `c_user` or `xs` has expired or will expire within a day. When a
page lands on `/login` or `/login.php` (a *login wall*), flnd waits and loads it again, at most
twice, then stops with "Facebook kept asking for a login; run flnd login fbk, then try again".

## How it fetches

Facebook's web app is called Comet. Its pages carry GraphQL results inside
`<script type="application/json">` blocks (the ones holding `__bbox`), and while a page scrolls
it asks its GraphQL endpoint for more, answered as one JSON result per line.

1. The `user` route loads the profile page once (`https://www.facebook.com/<user>`). It keeps
   the embedded results and the responses that came while the page loaded, and reads the
   profile header from them: the profile's id, its pictures, its story buckets, and the paths of
   its tabs.
2. **`posts`** reads that page's results first, without loading it again, then scrolls it and
   reads every captured response whose URL contains `graphql`.
3. **`photos`, `tagged`, `reels`, `videos`** each open their tab's page (the path the header
   names, such as `photos_by` or `photos`) and do the same. A tab the header does not list is
   skipped with "this profile has no such tab". The tabs not yet read are opened in background
   tabs meanwhile.
4. **`albums`** scrolls the albums tab, collects each album's link and title, then scrolls each
   album's page.
5. **`stories`** opens `https://www.facebook.com/stories/<bucket>/` for each story bucket the
   header lists.
6. **`profile`** takes the header's large profile picture and cover photo.

A video is saved from its DASH manifest when it has one (the best video and audio streams,
merged), else from its progressive files, HD first. Every other address the video has is kept
as a *mirror*, an address tried in turn when one refuses. A photo is its largest image URL.

A photo or video that the results leave undated is dated afterwards: flnd requests
`https://www.facebook.com/photo/?fbid=<id>` from inside the open page, so it carries the page's
session, and reads the first `created_time` in the answer. The engine asks this once per undated
post as its category ends, and again on `flnd sync` for files saved undated.

**Pace and concurrency.** `pace` is 0.2: at least 0.2 seconds between requests to Facebook's
domains, page loads included. Up to 8 files of a target download at once (the default
`concurrency`). The CDN gets a `Referer: https://www.facebook.com/` header, which it requires.

## Where files go

A target's files go under `.flnd/media/fbk/<target>/`, where `<target>` is the user name or id as
given. Inside it, the source declares:

- `folder = "{part}/{album}"`: `part` is where the file was found (its tab, or `videos`,
  `albums` or `stories`), and `album` the album's title when it has one. The `profile` category
  uses `profile`.
- `filename = "{date}_{id}"`: a file is `<date>_<photo or video id>.<ext>`, or `<id>.<ext>`
  without a date. The profile pictures are `profile_<photo id>.<ext>` and
  `cover_<photo id>.<ext>`.
- `key = "{id}"`: the history remembers `fbk:<photo or video id>`, so a photo found in two tabs
  is saved once. The `profile` category sets an empty key, so its history key is the file's path
  under the target's folder.

Example:

```
.flnd/media/fbk/example.page/photos/2026-01-02_1234567890.jpg
.flnd/media/fbk/example.page/albums/Holiday/2026-01-02_1234567891.jpg
```

## Quirks

- **Tab paths differ by profile.** A tab may be `photos_by` or `photos`, `reels_tab` or `reels`,
  `videos_by` or `videos`; flnd uses the one the header lists. Handled since 2026-11-11.
- **Progressive video files moved** under `videoDeliveryResponseFragment`; flnd reads both
  places. Handled since 2026-11-11.
- **Grid tiles hold small copies.** A tab's grid tile (`TimelineAppCollectionItem`) has a small
  image of a photo that an inner node has in full, so the tile is skipped. Handled since
  2026-11-11.
- **A video's thumbnails** are images inside the video's node; they are not saved as photos.
  Handled since 2026-11-11.
- **Other accounts' media** shows up in a profile's tabs; anything whose owner id is not the
  profile's is left out. Handled since 2026-11-11.
- **Image format from the URL.** The CDN converts an image to the format named in its
  `stp=dst-<format>` parameter, which gives the extension. Handled since 2026-11-11.
- **Temporary blocks.** Facebook rate-limits a session with a page saying "temporarily blocked",
  or a GraphQL answer that is the Comet error page (`CometErrorRoot.react`). flnd waits and
  tries again, at most 3 times, then stops with "Facebook says you've been temporarily blocked".
  Handled since 2026-11-12.
- **The next page's cursor** is looked for at most 3 objects deep under a result's `data`, so a
  comment list's `page_info` is not taken for the tab's. Handled since 2026-11-12.
- **A photo's own day** is taken to be the first `created_time` on its page. The code comment
  says this is unverified until a hand check on a live page. Handled since 2026-11-13.
- **Login wall paths** are matched on the first path segment, so a profile like `login.example`
  is not a wall. Handled since 2026-11-16.

## Limits

- **Single posts, photos, videos and reels are refused**: links under `/<profile>/posts`,
  `/watch`, `/reel`, `/reels`, `/videos`, `/photo`, `/photos`, `/groups`, `/events`, `/stories`
  and `/story.php` say "single posts and videos aren't supported yet; give the profile".
- **`fb.watch` links** have no route, so they are not read.
- **Stories** are only those active at run time.
- **Private content** is readable only when the login can see it.
- **No early stop.** Every tab is scrolled to its end on every run; files already saved are
  skipped, but the scroll still happens.
- **Without a profile header**, the `stories` and `profile` categories are skipped with a
  warning, and no media is left out as another account's.
