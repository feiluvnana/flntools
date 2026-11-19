# LOIBUS (`loi`)

flnd downloads one LOIBUS post at a time: a Markdown summary of the post (`info.md`), its preview
images, and, for a VIP account, its archives. The site is `loix.cc`, and its API is on
`api.moegoat.com`. The info and previews need no login. Archives need a logged-in VIP account,
and they count against its daily quota. The plugin fetches its pages over plain HTTP and never
opens a browser for them, since the site blocks one. A login can still use a browser window
(see [Logging in](#logging-in)). The plugin is `src/plugins/loi.py`, on plugin API 2.

A *target* is what you give `flnd get`; a *category* is one part of a target, named after
its last `/`. See the [glossary](../glossary.md) and the [CLI guide](../cli.md).

## Targets

| You type | Route | What it downloads |
| --- | --- | --- |
| `loi:1234567890` | `post` | One post, by its id |
| `https://loix.cc/lois/1234567890` | `post` | The same |
| `https://moegoat.com/lois/1234567890` | `post` | The same |

Each form reads one post: the route is single, so it never leads to another target.

A post that the API doesn't return, or that has no title, fails with "post <id> isn't there".

## Categories

| Category | What it holds |
| --- | --- |
| `photo` | `info.md` without download links, and the preview images |
| `archive` | `info.md` with the download links, and the archives (VIP quota applies) |

Without categories a post gets both; `loi:<id>/photo` names one. The categories run in the order
`archive`, then `photo`. `archive` writes `info.md` with the links, and the archives. `photo`
writes `info.md` without links only when `archive` isn't wanted in the run. So a run never
replaces an `info.md` that has links with one that hasn't.

`info.md` is a table of the post's fields (id, author, date, link, file size, VIP level needed,
media counts, and likes, views and downloads). It adds the unzip password and the tags when the
post has them, then the description, then its download sources (only with `archive`), then links
to its preview images.

## Logging in

The plugin declares a cookie login, `cookies("access-token")`, whose login page is the site. The
`access-token` cookie proves a login, and a login is not required. `flnd auth login loi <method>`
takes one of the cookie methods: `browser`, `chrome`, `file <path>`, `firefox` or `safari`. Left
out in a terminal, flnd asks for the method from a menu, and asks for the path when the method is
`file`.

- **`browser`**: opens a window of flnd's own Chromium at the site's login page. Log in there,
  and flnd reads the cookies once `access-token` is set. Close the window to give up.
- **`chrome`**: reads the cookies from your Chrome through flnd's Chrome extension.
- **`file <path>`**: reads a Netscape `cookies.txt` or a JSON export of cookies.
- **`firefox`** and **`safari`**: read the cookies from that browser. Safari is read on macOS
  only.

The cookies kept are those for `loix.cc` and `moegoat.com`. The login is sealed in the vault,
`metadata/secret.db` in the home folder. The key that opens it is kept in the user's key file,
outside the home. The plugin's `sign` hook sends the cookie's value as a Bearer token to the API.

Without a login, `photo` works in full. `archive` warns "archives need a login; run flnd auth
login loi" and writes `info.md` without links.

At the start of a run, flnd warns if the `access-token` cookie has expired, or expires within
3 days. An expired or refused token is not detected as such. The site gives no links, and flnd
warns that the site gives no download links for the post, naming the VIP level or the login it
needs.

## How it fetches

Every request is plain HTTP; there is no browser for the pages.

1. The post: `GET https://api.moegoat.com/api/lois/<id>`.
2. For `archive`, the account's VIP level: `GET https://api.moegoat.com/api/user/info`, read once
   per run, when the first archive needs it.
3. For `archive`, the links: `GET https://api.moegoat.com/api/user/loi/download_v6/<id>`.
4. Preview images and archives: from the image servers and mirrors the post names.

**Signing.** Every API request is signed the way the site's own JavaScript does it. The plugin's
`sign` hook adds four headers: `x-security-challenge`, `x-request-id` (a UUID-shaped id with a
short digest of the request hidden inside it), `x-signature-timestamp` and `x-request-signature`
(a SHA-256 digest of the method, the path and the time). The constants come from the site's
JavaScript. The engine calls `sign` before each try, so a retried request is signed again at its
own time. The token from the `access-token` cookie is sent as `Authorization: Bearer`.

**Headers.** Every request on the plugin's domains gets the site's `referer` and `origin`. A
post passes the same two headers to its preview images and archives, which are on other hosts.

**Encrypted links.** The API returns each download link, and the Baidu link and its password,
encrypted with AES-128-CBC under a key from the site's JavaScript. flnd decrypts them.

**Archives.** The links name one or more servers. Links that end in the same file name are
mirrors of one archive, tried in order. An archive is named by the last part of its URL, or
`archive.zip` when it has none.

**Daily quota.** A VIP account gets a number of archive posts a day by level: 15 (VIP1), 25
(VIP2), 40 (VIP3) or 60 (VIP4). An SVIP account gets twice that. An account with no VIP level gets
none. flnd counts each post whose links came back with archives, once, in the plugin's own state,
against the UTC day. When the count reaches the limit, or the API says the quota is used up,
`archive` skips the rest of the day's posts with a warning, without asking for their links. A
post already counted today is not counted again, and is not checked against the limit. When the
account's level can't be read, the limit is unknown and the links are asked for anyway.

**Lazy archives.** The `archive` category yields its post lazily. Its links are fetched, and the
post counted, only when the engine asks for the post's files, which it does for a post it will
download.

**Pace and concurrency.** `pace` is 0.5 (the default), so two page requests of one account are at
least half a second apart. The plugin sets no `limits`: each host's files download 8 at once,
adapting up to 32.

**Rate limits.** When the links answer is a rate limit in its JSON (a code of 429, or words that
say to slow down), the plugin's `blocked()` returns 0.0. The engine then holds the requests by its
block rounds: 30 seconds, then 2 minutes, then 8 minutes. A fourth round in a row fails the job as
blocked. An HTTP 429 gets the engine's own rate handling instead. A used-up quota is not a hold:
the plugin reads it and skips the day's remaining posts itself.

## Where files go

The class declares:

- `folder = "{author}/{post} {title}/{part}"`: `author` is the post's category name (`misc` when it
  has none), `title` its title, and `part` is `images` for the previews and empty otherwise.
- `filename = "{post}"`, which is never used: every file is named by the plugin. Previews are
  `01.jpg`, `02.jpg` and so on, keeping the URL's extension (`.jpg` when it has none). Archives
  keep their URL's file name (`archive.zip` when it has none).
- `key = "{path}"`: the history key is the file's path, as `loi:<path>`.

Every field is passed raw; the engine makes each folder and file name safe, so spaces, slashes
and colons become `_`.

Files go under `.flnd/media/loi/<id>/`:

```
.flnd/media/loi/1234567890/example_author/1234567890_Example_Title/info.md
.flnd/media/loi/1234567890/example_author/1234567890_Example_Title/images/01.jpg
.flnd/media/loi/1234567890/example_author/1234567890_Example_Title/example_archive.zip
```

`info.md` is written with the links when `archive` runs, and without them when only `photo` runs.

## Quirks

- **Signed API.** Every API request carries the signature headers the web client adds.
- **Encrypted links.** Download links and passwords come AES-encrypted. A link that won't
  decrypt fails with "the site changed its key".
- **Image and mirror 403s.** The image servers and archive mirrors answer 403 without the site's
  `referer` and `origin`, so every such file carries them.
- **The token cookie.** `access-token` may be %-encoded and may already start with `Bearer `.
  Both are removed before use.
- **Quota and rate-limit answers.** The API reports both as JSON, sometimes in Chinese. flnd reads
  an HTTP 429, a JSON code of 429, or words like "rate" and "too many" as a rate limit. It reads
  an error (an HTTP error status, a JSON status of error or fail, or a code of 403) with words like
  "quota", "limit" or "exceeded" as the daily quota.
- **Quota before time.** The archive post is lazy, so a post outside `--when` asks for no links
  and uses no quota.

## Limits

- One post per target: there is no author, tag or search target. Only the forms above are read;
  other links on the site are not targets. The plugin declares no `unsupported` links.
- A post the account's VIP level can't open gives no links, and the warning names the level it
  needs.
- The daily count is kept by the UTC day, which may not be the day the site resets its quota.
- A post is counted as soon as its links are fetched, even if its archives then fail to download.
- An archive's name is known only from its links, so the engine tells an archive post already
  saved by its own rows in `flnd.db` (its target, post id and category), and does not ask for its
  links again. `--again` asks anyway. A post whose archives all failed has no rows, so it is asked
  for again on the next run.
- Baidu links are written to `info.md` only. flnd doesn't download from Baidu.

## Testing

Unit tests fake the network, the browser and the clock. Live checks are done by hand, on a test
account, and never in unit tests. A change to this plugin must keep the routes, the two
categories and their order, the daily quota rules, the signing and decryption, and the history
key (`loi:<path>`), so that files already saved are not saved again.
