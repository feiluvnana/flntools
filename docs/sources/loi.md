# LOIBUS (`loi`)

flnd downloads one LOIBUS post at a time: a Markdown summary of the post (`info.md`), its preview
images, and, for a VIP account, its archives. The site is `loix.cc`, and its API is on
`api.moegoat.com`. The info and previews need no login; archives need a logged-in VIP account and
count against its daily quota. The source never opens a browser: a code comment says the site
blocks one.

A *target* is what you give `flnd download`; a *category* is one part of a target, chosen with
`--only`. See the [glossary](../glossary.md) and the [CLI guide](../cli.md).

## Targets

| You type | Route | What it downloads |
| --- | --- | --- |
| `loi:1234567890` | `post` | one post, by its id |
| `https://loix.cc/lois/1234567890` | `post` | the same |
| `https://moegoat.com/lois/1234567890` | `post` | the same |

A post that the API doesn't return, or that has no title, fails with "post <id> isn't there".

## Categories

| Category | What it holds |
| --- | --- |
| `photo` | `info.md` without download links, and the preview images |
| `archive` | `info.md` with the download links, and the archives (VIP quota applies) |

Without `--only` a post gets every category; `--only` names one or both. When both run,
`archive` runs second and its `info.md`, with links, replaces the one `photo` wrote.

`info.md` is a table of the post's fields (id, author, date, link, file size, VIP level needed,
media counts, likes, views and downloads), its unzip password and tags when it has them, its
description, its download sources (in `archive` only) and links to its preview images.

## Logging in

The source declares `login = cookies("access-token")`: the `access-token` cookie proves a
login, and it isn't required. `flnd login loi <how>` takes a browser (read the cookies of an
installed browser, such as `chrome`) or a cookie file's path (a Netscape `cookies.txt` or JSON
cookie file). The source's `sign` hook sends the cookie's value as a Bearer token to the API.

Without a login, flnd warns once that it is using public access. `photo` works in full. `archive`
warns "archives need a login" and writes `info.md` without links.

When a run starts, flnd warns if the `access-token` cookie has expired or expires within 24
hours, and `flnd login` shows the same. An expired or refused token is not detected as such:
the site gives no links, and flnd warns that the post's VIP level or a login is needed.

## How it fetches

Every request is plain HTTP; there is no browser.

1. The post: `GET https://api.moegoat.com/api/lois/<id>`.
2. For `archive`, the account's VIP level: `GET https://api.moegoat.com/api/user/info`.
3. For `archive`, the links: `GET https://api.moegoat.com/api/user/loi/download_v6/<id>`.
4. Preview images and archives: from the image servers and mirrors the post names.

**Signing.** Every API request is signed the way the site's own JavaScript does it. The
source's `sign` hook adds four headers: `x-security-challenge`, `x-request-id` (a UUID-shaped id
with a short digest of the request hidden inside it), `x-signature-timestamp` and
`x-request-signature` (a SHA-256 digest of the method, the path and the time). The constants
come from the site's JavaScript. The engine calls `sign` before each try, so a retried request
is signed again at its own time.

**Headers.** Every request on the source's domains gets the site's `referer` and `origin`. A
post passes the same two headers to its preview images and archives, which are on other hosts.

**Encrypted links.** The API returns each download link, and the Baidu link and its password,
encrypted with AES-128-CBC under a key from the site's JavaScript. flnd decrypts them.

**Archives.** The links name one or more servers; links that end in the same file name are
mirrors of one archive, tried in order. An archive is named by the last part of its URL, or
`archive.zip`.

**Daily quota.** A VIP account gets a number of archive posts a day by level: 15 (VIP1), 25
(VIP2), 40 (VIP3) or 60 (VIP4), twice that for an SVIP account; an account with no VIP level
gets none. flnd counts each post whose links it fetched, once a day, in its own state for the
source. When the count reaches the limit, or the API says the quota is used up, `archive` skips
the rest of the day's posts with a warning, without asking for their links. A post already
counted today is not counted again, and costs no check of the account's level. When the
account's level can't be read, the limit is unknown and the links are asked for anyway.

**Lazy archives.** The `archive` category yields its post lazily: the links are fetched, and the
post counted, only when the engine asks for the post's files, which it does only for a post
inside `--when`.

**Pacing:** `pace` is 0.5, so requests to the source's domains are at least half a second apart.
It sets no `limits`: each host's files download 8 at once, adapting up to 32. When the links request is
refused as a rate limit (an HTTP 429, or JSON that says so), flnd waits with the engine's
back-off (30 s, 2 min, 8 min) and asks again; a fourth rate limit in a row stops the target as
blocked. The other early stops are the engine's; a post is one target, so a sync of it has no
listing to cut short.

## Where files go

The class declares:

- `folder = "{author}/{post} {title}/{part}"`: `author` is the post's category name (`misc` when it
  has none), `title` its title, and `part` is `images` for the previews and empty otherwise.
- `filename = "{post}"`, which is never used: every file is named by the post. Previews are
  `01.jpg`, `02.jpg` and so on (keeping the URL's extension), and archives keep their URL's file name.
- `key = ""`: the history key is the file's path, as `loi:<path>`.

Every field is passed raw; the engine makes each folder and file name safe, so spaces, slashes
and colons become `_`.

Files go under `.flnd/media/loi/<id>/`:

```
.flnd/media/loi/1234567890/example_author/1234567890_Example_Title/info.md
.flnd/media/loi/1234567890/example_author/1234567890_Example_Title/images/01.jpg
.flnd/media/loi/1234567890/example_author/1234567890_Example_Title/example_archive.zip
```

`info.md` is written again on every run.

## Quirks

- **Signed API.** Every API request carries the signature headers the web client adds. Handled
  since 2026-11-11.
- **Encrypted links.** Download links and passwords come AES-encrypted. A link that won't decrypt
  fails with "the site changed its key". Handled since 2026-11-11.
- **Image and mirror 403s.** The image servers and archive mirrors answer 403 without the site's
  `referer` and `origin`. Handled since 2026-11-11.
- **The token cookie.** `access-token` may be %-encoded and may already start with `Bearer `;
  both are removed before use. Handled since 2026-11-11.
- **Quota and rate-limit answers.** The API reports both as JSON, sometimes in Chinese. flnd reads
  an HTTP 429, a JSON code of 429, or words like "rate" and "too many" as a rate limit, and an
  error (an HTTP error status, a JSON status of error or fail, or a code of 403) with words like
  "quota", "limit" or "exceeded" as the daily quota. Handled since 2026-11-15.
- **Quota before time.** The archive post is lazy, so a post outside `--when` asks for no links
  and uses no quota. Handled since 2026-11-12.

## Bugs fixed (2026-11-16)

- An account with no VIP level has a known daily limit of 0. It was read as "unknown", so
  the links were asked for anyway; now `archive` warns "the account has no VIP level" and asks
  for none.
- A 429 in the daily-quota test could never match, since a 429 was read as a rate limit first,
  and a quota refusal by HTTP status alone wasn't read at all. Now a rate limit always backs off,
  and a daily-quota answer, by its JSON or its HTTP status, marks the day used up.
- The archive post's links were fetched, and the post counted, while listing, before the engine
  could skip the post. The post is now lazy: see "Lazy archives".

## Limits

- One post per target: there is no author, tag or search target. Any other link is refused with
  the forms above; the source declares no `unsupported` links.
- Archives need a VIP level at least the post's own. A post the account can't open gives no
  links, with a warning naming the level needed.
- The daily count is kept by this machine's calendar day, which may not be the day the site
  resets its quota.
- A post is counted as soon as its links are fetched, even if its archives then fail to
  download.
- A post's files are keyed by their paths, and an archive's name is known only from its links.
  So the engine tells an archive post already saved by its own rows in `flnd.db` (its target,
  post id and category), and doesn't ask for its links again; `--again` does. A post whose
  archives all failed has no rows, and asks again on the next run.
- Baidu links are written to `info.md` only; flnd doesn't download from Baidu.
