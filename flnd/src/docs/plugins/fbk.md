# Facebook (`fbk`)

flnd downloads the photos and videos of a Facebook profile or page: its posts, photos, tagged
photos, albums, reels, videos, stories, and its profile and cover pictures. It also downloads a
single video, reel, photo or post by its link. The plugin reads the pages in a browser, by
loading them and scrolling. Its login is optional, but Facebook shows a logged-out browser
little. The plugin is `src/plugins/fbk.py`, on plugin API 2.

## Targets

A *target* is what you ask flnd to download (see the [glossary](../glossary.md)). A *route* is
the part of the plugin that reads one kind of target.

| You type | Route | What it downloads |
| --- | --- | --- |
| `fbk:example.page` | `user` | The profile's categories (below) |
| `https://www.facebook.com/example.page` | `user` | The same |
| `…/example.page/photos` | `user` | The same; the part after the name is ignored |
| `https://www.facebook.com/@example.page` | `user` | The same |
| `https://www.facebook.com/profile.php?id=1234567890` | `user` | The profile with that id |
| `https://www.facebook.com/people/example-name/1234567890` | `user` | The profile with that id |
| `fbk:1234567890` | `user` | The profile with that id |
| `https://www.facebook.com/watch/?v=1234567890` | `video` | That video |
| `https://www.facebook.com/example.page/videos/1234567890` | `video` | That video |
| `…/videos/<id>`, `…/reel/<id>`, `…/reels/<id>` | `video` | That video or reel |
| `https://fb.watch/aBc-12/` | `watch` | The video the short link leads to |
| `https://www.facebook.com/photo.php?fbid=1234567890` | `photo` | That photo; `/photo/?fbid=` too |
| `…/permalink.php?story_fbid=<post>&id=<owner>` | `post` | The post's photos and videos |

Every `facebook.com` form also works on `fb.com`. The site's own pages (`photo.php`,
`permalink.php`, `story.php`, `profile.php`, `watch`, `reel`, `videos`, `people`, `login`, …)
are never read as a profile's name, not even typed as `fbk:photo.php`.

A single link's files go in its own target folder, without a category:

- **`video`** opens `https://www.facebook.com/watch/?v=<id>` and keeps only that video, not the
  suggestions beside it.
- **`watch`** opens the `fb.watch` link in the browser (a private video's redirect needs the
  login), reads the video's id from the page it lands on (`/watch/?v=<id>`, `/reel/<id>` or
  `/<page>/videos/<id>`), then reads that video as `video` does. A link that lands on a login
  wall is waited out as any wall is (see [Logging in](#logging-in)). One that lands anywhere
  else, or on a page with no video id, stops with "didn't lead to a video". The target's folder
  is named after the short code, but the file's history key is the video's id, so the same video
  by another link is not saved twice.
- **`photo`** opens `https://www.facebook.com/photo/?fbid=<id>` and keeps only that photo. A
  `photo.php` link without a numeric `fbid=` is refused with what it lacks.
- **`post`** opens the permalink and keeps every photo and video the post's owner owns, leaving a
  comment's photo out. Its target value is `<owner id>_<story_fbid>`. A permalink without both
  `story_fbid=` and `id=` is refused with what it lacks.

A page that shows none of the media asked for (private, deleted, or needing a login) stops with
"shows no photo or video".

## Categories

A *category* is one part of a profile that can be downloaded on its own. Without a category
named, a profile gets its own content: every category except `tagged`, which is other people's
media and is taken only when named, or with `/all`. `fbk:<user>/reels` names one or more (see
[the CLI guide](../cli.md)).

| Category | What it holds |
| --- | --- |
| `posts` | Photos and videos of the profile's own posts (the profile page itself) |
| `photos` | The photos tab |
| `tagged` | Photos the profile is tagged in (other people's media, so `own=False`) |
| `albums` | Every album on the albums tab, each in its own folder |
| `reels` | The reels tab |
| `videos` | The videos tab |
| `stories` | The stories active now |
| `profile` | The profile picture and the cover photo |

No category is newest first (`newest_first=False` on the `user` route, which its categories
take). Each tab is read to its end on every run. The engine's early stops (a sync that has
caught up, `--when`) apply only to a newest-first listing, so they never end one of these. The
plugin ends a scroll only where the site does. Whether a tab lists newest first has not been
confirmed on a live page.

Media another account owns is left out of a tab, when the profile header gives the profile's id.
A video found in the `posts`, `photos` or `tagged` tab is filed under `videos/`, and a photo that
names its album under `albums/<album>/`, whichever tab found it.

## Logging in

`flnd auth login fbk <method>` logs in. The methods are `browser`, `chrome`, `file <path>`,
`firefox` and `safari`, and they are the only ones the plugin declares (`cookies(...)` in
`fbk.py`). Left out in a terminal, flnd asks for the method from a menu, and asks for the path
when the method is `file`.

- **`browser`**: opens a window of flnd's own Chromium at Facebook's login page. Log in there,
  and flnd reads the cookies once one of the login cookies is set. Close the window to give up.
- **`chrome`**: reads the cookies from your Chrome through flnd's Chrome extension (see
  `flnd extension install`).
- **`file <path>`**: reads a Netscape `cookies.txt` or a JSON export of cookies.
- **`firefox`** and **`safari`**: read the cookies from that browser. Safari is read on macOS
  only.

The login is accepted when the cookies hold `c_user` or `xs`; either one is enough. Only the
cookies for Facebook's domains are kept. The login is sealed in the vault, `metadata/secret.db`
in the home folder. The key that opens it is kept in the user's key file, outside the home.

Without a login, flnd opens the same pages and gets what Facebook shows a logged-out visitor.

At the start of a run, flnd warns if `c_user` or `xs` has expired, or expires within 3 days.
When a page lands on `/login` or `/login.php` (a *login wall*, declared as the plugin's `walls`),
the account's requests wait 30 seconds, then 120 seconds, and the page is loaded again. After the
second wait the job moves to another account if there is one. Otherwise it fails with a login
error, and the account is marked expired.

## How it fetches

Facebook's web app is called Comet. Its pages carry GraphQL results inside
`<script type="application/json">` blocks (the ones holding `__bbox`). While a page scrolls, it
asks its GraphQL endpoint for more, and each answer is one JSON result per line.

1. The `user` route loads the profile page once (`https://www.facebook.com/<user>`). It keeps
   the embedded results and the responses that came while the page loaded, and reads the
   profile header from them: the profile's id, its pictures, its story buckets, and the paths of
   its tabs. Each tab's URL goes to its category as its `url=`, so the engine can open it ahead
   in a warm tab. A URL is empty when the header lacks that tab; without a header, each tab is
   tried at its first path. If the profile page doesn't load, the route raises an error.
2. **`posts`** reads that page's results first, without loading it again, then scrolls it and
   reads every captured response whose URL contains `graphql`. The scroll ends when the tab's
   `page_info` says there is no next page (`tab_cursor`), but only after an earlier page said
   there was one. On some loads the profile's Tiles feed (its intro photos) answers first with
   "no next page", which is not the tab's end (seen 2026-11).
3. **`photos`, `tagged`, `reels`, `videos`** each open their tab's page (the path the header
   names, such as `photos_by` or `photos`) and do the same. A tab the header does not list is
   skipped with "this profile has no such tab".
4. **`albums`** scrolls the albums tab, collects each album's link and title, then scrolls each
   album's page. The album pages are started in warm tabs (`ctx.warm`) before the first is read.
5. **`stories`** opens `https://www.facebook.com/stories/<bucket>/` for each story bucket the
   header lists, one page each. The bucket pages are started in warm tabs (`ctx.warm`) before
   the first is read. A bucket that doesn't load fails the `stories` category.
6. **`profile`** takes the header's large profile picture and cover photo.

A video is saved from its DASH manifest when it has one (the best video and audio streams,
merged). Otherwise its progressive files are kept in turn as mirrors, an address tried in turn
when one refuses, HD first, and so are its other video addresses. A photo is its largest image
URL.

A photo or video that the results leave undated is dated afterwards. flnd requests
`https://www.facebook.com/photo/?fbid=<id>` from inside the open page, so the request carries the
page's session, and reads the first `created_time` in the answer. The engine asks for the day of an
undated post. A post the page gives no day for is not asked again for 30 days, and asking stops
after a block. A block answer to this request is held like any other
(`blocked()`). This has not been checked on a live page.

**Pace and concurrency.** `pace` is 0.2: at least 0.2 seconds between two page requests of one
account. Each CDN host's files download 8 at once, adapting up to 32. The plugin sets no `limits`
of its own. The CDN requires a `Referer: https://www.facebook.com/` header, which the plugin
sends with every file.

## Where files go

A target's files go under `.flnd/media/fbk/<target>/`, where `<target>` is the user name or id as
given. Inside it, the plugin declares:

- `folder = "{part}/{album}"`: `part` is where the file was found (its tab, or `videos`,
  `albums` or `stories`), and `album` the album's title when it has one. The `profile` category
  uses `profile`. An empty part or album is left out of the path.
- `filename = "{date}_{post}"`: a file is `<date>_<photo or video id>.<ext>`, or `<id>.<ext>`
  without a date. The profile pictures are `profile_<photo id>.<ext>` and
  `cover_<photo id>.<ext>`. A DASH video gets no name from the plugin: the engine names it from
  `filename`, and gives the joined file `.webm` when both its streams are WebM, else `.mp4`.
- `key = "{post}"`: the history remembers `fbk:<photo or video id>`, so a photo found in two tabs
  is saved once. The `profile` category sets `key = "{path}"`, so its history key is the file's
  path under the target's folder, and a new picture is a new file.

Example:

```
.flnd/media/fbk/example.page/photos/2026-01-02_1234567890.jpg
.flnd/media/fbk/example.page/albums/Holiday/2026-01-02_1234567891.jpg
```

## Quirks

- **Tab paths differ by profile.** A tab may be `photos_by` or `photos`, `reels_tab` or `reels`,
  `videos_by` or `videos`. flnd uses the one the header lists.
- **Progressive video files** sit under `videoDeliveryResponseFragment`. flnd reads that place
  and the older one on the node.
- **Grid tiles hold small copies.** A tab's grid tile (`TimelineAppCollectionItem`) has a small
  image of a photo that an inner node has in full, so the tile is skipped.
- **A video's thumbnails** are images inside the video's node. They are not saved as photos.
- **Other accounts' media** shows up in a profile's tabs. Anything whose owner id is not the
  profile's is left out.
- **Image format from the URL.** The CDN converts an image to the format named in its
  `stp=dst-<format>` parameter, which gives the extension.
- **Temporary blocks.** Facebook rate-limits a session with a page that says "temporarily
  blocked" and embeds no results, or with a GraphQL line that is the Comet error page
  (`CometErrorRoot.react`). Each one is held by the engine's block rules, with the reason
  "Facebook says you've been temporarily blocked": 30 seconds, then 120, then 480. A fourth
  block in a row fails that job with a block error. The plugin only reports each block; the
  waits are the engine's.
- **The next page's cursor** is looked for at most 3 objects deep under a result's `data`, so a
  comment list's `page_info` is not taken for the tab's.
- **A photo's own day** is taken to be the first `created_time` on its page. This has not been
  checked on a live page; a wrong guess only dates a photo that would otherwise be undated.
- **Login wall paths** are matched as whole path segments, so a profile named `login.example` is
  not a wall.
- **`fb.watch` short links** are followed in the browser to the video they point to.
- **`photo.php` and `permalink.php`** are the `photo` and `post` routes, or refused. They are
  never read as user names.

## Limits

- **Some links are refused**: `/<profile>/posts` (and `/posts/<id>`), and the site's own
  `/photos`, `/groups`, `/events` and `/stories` pages, say "this kind of link isn't supported
  yet; give the profile, or a video, photo.php or permalink.php link". Any path after a profile's
  name, such as `/photos` or `/reels`, is ignored, and the profile is read.
- **Stories** are only those active at run time.
- **Private content** is readable only when the login can see it.
- **No early stop.** No tab is newest first, so the engine's early stops never end one: every
  tab is read to its end on every run. Files already saved are skipped, but the scroll still
  happens.
- **Without a profile header**, the `stories` and `profile` categories are skipped with a
  warning, and no media is left out as another account's. Every tab is tried at its first path.

## Testing

Unit tests fake the network, the browser and the clock. Live checks are done by hand, on a test
account, and never in unit tests. A change to this plugin must keep the routes and refusals
above, the `own` flag of each category, the wall and block waits, and the history keys (`fbk:<id>`
for media, `{path}` for profile pictures), so that files already saved are not saved again.
