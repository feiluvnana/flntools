# The web (`web`)

flnd downloads any link with the `web` plugin when no other plugin claims it: a file as it is, an
HLS (`.m3u8`) or DASH (`.mpd`) stream by its playlist, or the videos and images a page shows. It
also takes the media the Chrome extension's popup lists on a tab, which sends each item's URL
here. It owns no domain, needs no login and no browser, and reads everything over plain HTTP.

It is built on plugin API 2 (`src/plugins/web.py`). The engine hands it every link that no other
plugin's domains claim.

A *target* is what you give flnd. See the [glossary](../glossary.md) and the
[CLI guide](../cli.md).

## Targets

| You type | Route | What it downloads |
| --- | --- | --- |
| `web:https://example.com/clip.mp4` | `page` | the file |
| `web:https://example.com/hls/master.m3u8` | `page` | the stream, as one video |
| `web:https://example.com/watch/42` | `page` | what the link is: a file, a stream, or a page's media |

A link needs its `http://` or `https://`: `example.com/clip.mp4` alone is no link. Until the
resolver of PLAN.md step 10 reads a link no plugin owns as `web`'s, such a link is refused
with a hint to write `web:<link>`. A link on another plugin's domains goes to that plugin;
write `web:<link>` to take it here instead. A link's `#fragment` stays in the target's key, but
is never sent, and is no part of the history key.

Every link is one post (`single`), so it is never followed, and it has no categories.

## Logging in

The plugin has no login (`login = None`) and owns no domain, so it sends no cookies, and a 401
or 403 is a refusal like any other, never a login wall.

## How it fetches

A link is read only when it is new: its post is lazy, so a saved link costs no request.

1. **By its extension.** A link whose path ends in `.m3u8` or `.mpd` is a stream
   (`File.stream`); one ending in a file's extension (`.jpg`, `.mp4`, `.zip`, `.pdf` and the
   rest of `FILES`) is the file. Neither asks the server anything first.
2. **Else, by a `HEAD` request.** Its `Content-Type` decides: an HLS or DASH type
   (`application/vnd.apple.mpegurl`, `application/dash+xml`, ...) is a stream, `text/html` is a
   page, and anything else, or none, is a file, its `Content-Length` its size. A 404 or 410 is
   `NotFound`. HEAD comes first so that a large file with no extension is never read into
   memory to find out what it is.
3. **A server that refuses `HEAD`** (any answer that isn't a success, such as 405, 501, or the
   403 of an S3 or CloudFront presigned link) gets a `GET` of one byte (`Range: bytes=0-0`). A type that isn't HTML is the file, its
   size read from `Content-Range`; only an HTML answer is read as a page, and an HTML 206 (the
   page cut to one byte) is fetched again whole. A server that ignores `Range` and sends a
   large body that isn't HTML is still read in full: a cap on that belongs in the engine's
   fetch (step 7).
4. **A page** is fetched once with `GET`, and its media taken in this order, each URL once:
   - each `og:video` (or `og:video:url`), its `og:video:secure_url` when it has one, leaving
     out those whose `og:video:type` is `text/html` or Flash (parameters ignored), which are
     players, not files;
   - each `<video src>` and `<video><source src>`;
   - each `og:image` (or `og:image:url`), its `secure_url` when it has one.

   An `og:video` and its `secure_url` are one item in either order; another `og:*:url` starts
   a new one, and a `type` or `width` with no item before it says nothing. A `secure_url` that
   isn't an http(s) link leaves its `og:video`. Relative URLs join to an http(s) `<base href>`,
   else to where the page landed. Each URL is kept once without its fragment, and blank values,
   `#`, `blob:` and `data:` URLs, and the page or the link itself (with or without a
   `user:pw@`) are left out. A playlist among them is a stream. A page with none is `NotFound`.

Each request is a file job, since the plugin owns no domain: it is paced by the host's file cap,
not by `pace`. Every file is fetched with the link as its `Referer` (the post's headers), as a
browser fetches a page's media; a stream found on a page gets the URL the page landed on.

## Where files go

The class declares:

- `folder = "{category}"`, the default: the route has no category, so a link's files go straight
  into its target folder.
- `filename = "{name}"`: `name` is the link's last path part without its extension (`clip`,
  `42`), or its host when that stem is blank or only dots (`example.com` for `/`, `..`,
  `.mp4`, `%20.mp4` or `%2e%2e.mp4`); an IPv6 host is written with dashes (`[--1]` for
  `[::1]`). A file's extension comes from its content type, then from its URL. A second file of
  the same name takes its number (`42_2.mp4`).
- `key = "{post}_{num}"`: the post is the link without its fragment or any `user:pw@`, an
  empty path written `/`, so the history key is `web:<link>_<n>`, where `n` is the file's place
  in the page from 0. The same link is the files' `Referer`, and the one an error names; the
  fetch goes to the link as written.

Files go under `.flnd/media/web/<target>/`, where `<target>` is the link made a safe name. A
stream is saved as `.mp4`.

```
.flnd/media/web/https_example.com_watch_42/42.mp4
.flnd/media/web/https_example.com_watch_42/42.jpg
```

## Limits

- No login and no browser: a page that builds its media with scripts, or hides it behind a login,
  shows none here. Use the site's own plugin, or the extension's popup on the open tab.
- Streams locked with a key (AES or DRM) can't be read (`flnkit/stream.py`).
- A stream found by its `Content-Type` alone (no `.m3u8` or `.mpd` in its path) needs the engine
  to read the playlist's type from its answer.
- Only Open Graph tags and `<video>` are read: no `twitter:` cards, `<img>` tags, `<audio>` or
  JSON-LD.
- Files are undated.
- The target's own key is the link as typed, `user:pw@` included, until the resolver cleans
  it (step 7); only the post and history keys drop it here.
- A page may point its media at a private address (`localhost`, `10.0.0.1`), and it is
  fetched, as yt-dlp does.
