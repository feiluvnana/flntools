# The web (`web`)

flnd downloads any link with the `web` plugin when no other plugin claims it. The plugin takes a
file as it is, an HLS (`.m3u8`) or DASH (`.mpd`) stream by its playlist, or the videos and images
a page shows. The Chrome extension's popup also lists the media of a tab, and sends each item's
URL here. The plugin owns no domain, needs no login and no browser, and reads everything over
plain HTTP. It is built on plugin API 2 (`src/plugins/web.py`).

A *target* is what you give flnd. See the [glossary](../glossary.md) and the
[CLI guide](../cli.md).

## Targets

| You type | Route | What it downloads |
| --- | --- | --- |
| `https://example.com/clip.mp4` | `page` | the file |
| `https://example.com/hls/master.m3u8` | `page` | the stream, as one video |
| `https://example.com/watch/42` | `page` | what the link is: a file, a stream, or a page's media |
| `web:https://example.com/watch/42` | `page` | the same, when another plugin would claim the host |

A link on no other plugin's domains is read by `web` with or without the `web:` prefix. A link
on another plugin's domains goes to that plugin, and `web:<link>` takes it here instead. The
`web:` prefix needs a link with its `http://` or `https://`: `web:example.com/clip.mp4` is
refused. A link's `#fragment` is never sent, and it is no part of the history key.

Every link is one post (`single`), so it is never followed, and it has no categories.

## Logging in

The plugin has no login (`login = None`) and owns no domain. It sends no cookies, and a 401 or 403
is a refusal like any other, never a login wall.

## How it fetches

A link is read only when it is new: its post is lazy, so a saved link costs no request.

1. **By its extension.** A link whose path ends in `.m3u8` or `.mpd` is a stream
   (`File.stream`). One ending in a file's extension (`.jpg`, `.mp4`, `.zip`, `.pdf` and the
   rest of `FILES`) is the file. Neither asks the server anything first.
2. **Else, by a `HEAD` request.** Its `Content-Type` decides. An HLS or DASH type
   (`application/vnd.apple.mpegurl`, `application/dash+xml`, ...) is a stream. `text/html` is a
   page. Anything else, or no type, is a file, and its `Content-Length` is its size. A 404 or 410
   is `NotFound`. HEAD comes first, so that a large file with no extension is never read into
   memory to find out what it is.
3. **A server that refuses `HEAD`** (any answer that isn't a success, such as 405, 501, or the
   403 of an S3 or CloudFront presigned link) gets a `GET` of one byte (`Range: bytes=0-0`). A
   type that isn't HTML is the file, and its size is read from `Content-Range`. Only an HTML
   answer is read as a page. An HTML 206 (the page cut to one byte) is fetched again whole. A
   server that ignores `Range` sends its whole body. The plugin sets no cap of its own.
4. **A page** is fetched once with `GET`, and its media is taken in this order, each URL once:
   - each `og:video` (or `og:video:url`), or its `og:video:secure_url` when it has one, leaving
     out those whose `og:video:type` is `text/html` or Flash (parameters ignored), which are
     players, not files;
   - each `<video src>` and `<video><source src>`;
   - each `og:image` (or `og:image:url`), or its `secure_url` when it has one.

   An `og:video` and its `secure_url` are one item in either order. Another `og:*:url` starts a
   new one, and a `type` or `width` with no item before it says nothing. A `secure_url` that
   isn't an http(s) link falls back to the item's `url`. Relative URLs join to an http(s)
   `<base href>`, else to where the page landed. Each URL is kept once without its fragment.
   Blank values, `#`, `blob:` and `data:` URLs are left out, and so are the page itself and the
   link itself, with or without a `user:pw@`. A playlist among them is a stream. A page with none
   is `NotFound`.

Each file is a file job, since the plugin owns no domain: the host's file cap paces it. Every file
is fetched with the link as its `Referer`, as a browser fetches a page's media. A stream found on
a page gets the URL the page landed on as its `Referer`.

## Where files go

The class declares:

- `folder = "{category}"`, the default: the route has no category, so a link's files go straight
  into its target folder.
- `filename = "{name}"`: `name` is the link's last path part without its extension (`clip`,
  `42`), or its host when that stem is blank or only dots (`example.com` for `/`, `..`, `.mp4`,
  `%20.mp4` or `%2e%2e.mp4`). An IPv6 host is written with dashes (`[--1]` for `[::1]`). A file's
  extension comes from its content type, then from its URL. A second file of the same name takes
  its number (`42_2.mp4`).
- `key = "{post}_{num}"`: the post is the link without its fragment or any `user:pw@`, with an
  empty path written `/`. So the history key is `web:<link>_<n>`, where `n` is the file's place
  in the page, from 0. The same link is the files' `Referer`, and the one an error names. The
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
- A stream is found by its `.m3u8` or `.mpd` path, or by the Content-Type of its `HEAD` answer.
  A server that sends the wrong type for a stream without such a path shows it as a file.
- Only Open Graph tags and `<video>` are read: no `twitter:` cards, `<img>` tags, `<audio>` or
  JSON-LD.
- Files are undated.
- The post's history key and its `Referer` drop any `user:pw@`. A link typed with one keeps it in
  the target as typed.
- A page may point its media at a private address (`localhost`, `10.0.0.1`), and it is fetched,
  as yt-dlp does.

## Testing

Unit tests fake the network, the browser and the clock. Live checks are done by hand, on a test
link, and never in unit tests. A change to this plugin must keep the extension rules, the `HEAD`
and `Range` fallbacks, the page media order above, and the history key (`web:<link>_<n>`), so that
files already saved are not saved again.
