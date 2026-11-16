# LOIBUS (`loi`)

flnd downloads one LOIBUS post at a time: a Markdown summary of the post (`info.md`), its preview
images, and, for a VIP account, its archives. The site is `loix.cc`, and its API is on
`api.moegoat.com`. The info and previews need no login; archives need a logged-in VIP account and
count against its daily quota. The source never opens a browser: a code comment says the site
blocks one.

A *target* is what you give `flnd download`; a *category* is one part of a target, chosen with
`--from`. See the [glossary](../glossary.md) and the [CLI guide](../cli.md).

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

There is no default category: `--from` is required, with one or both, or `all`. When both run,
`archive` runs second and its `info.md`, with links, replaces the one `photo` wrote.

`info.md` is a table of the post's fields (id, author, date, link, file size, VIP level needed,
media counts, likes, views and downloads), its unzip password and tags when it has them, its
description, its download sources (in `archive` only) and links to its preview images.

## Logging in

`flnd auth login` offers two methods: `browser` (read the cookies of an installed browser) and
`file` (a Netscape `cookies.txt` or JSON cookie file). The `access-token` cookie proves a login;
flnd sends its value as a Bearer token to the API.

Without a login, flnd warns once that it is using public access. `photo` works in full. `archive`
warns "archives need a login" and writes `info.md` without links.

When a run starts, flnd warns if the `access-token` cookie has expired or expires within 24
hours, and `flnd auth ls` shows the same. An expired or refused token is not detected as such:
the site gives no links, and flnd warns that the post's VIP level or a login is needed.

## How it fetches

Every request is plain HTTP; there is no browser.

1. The post: `GET https://api.moegoat.com/api/lois/<id>`.
2. For `archive`, the account's VIP level: `GET https://api.moegoat.com/api/user/info`.
3. For `archive`, the links: `GET https://api.moegoat.com/api/user/loi/download_v6/<id>`.
4. Preview images and archives: from the image servers and mirrors the post names.

**Signing.** Every API request is signed the way the site's own JavaScript does it. flnd adds
four headers: `x-security-challenge`, `x-request-id` (a UUID-shaped id with a short digest of the
request hidden inside it), `x-signature-timestamp` and `x-request-signature` (a digest of the
method, the path and the time). The constants come from the site's JavaScript.

**Headers.** Every request on the source's domains gets the site's `referer` and `origin`. A
post passes the same two headers to its preview images and archives, which are on other hosts.

**Encrypted links.** The API returns each download link, and the Baidu link and its password,
encrypted with AES-128-CBC under a key from the site's JavaScript. flnd decrypts them.

**Archives.** The links name one or more servers; links that end in the same file name are
mirrors of one archive, tried in order. An archive is named by the last part of its URL, or
`archive.zip`.

**Daily quota.** A VIP account gets a number of archive posts a day by level: 15 (VIP1), 25
(VIP2), 40 (VIP3) or 60 (VIP4), twice that for an SVIP account. flnd counts each post whose links
it fetched, once a day, in its own state for the source. When the count reaches the limit, or the
API says the quota is used up, `archive` skips the rest of the day's posts with a warning. A post
already counted today is not counted again.

**Pacing:** `pace` is 0.5, so requests to the source's domains are at least half a second apart.
`concurrency` is 8: up to 8 files of one target download at once. When the links request is
refused as a rate limit, flnd backs off and tries once more.

## Where files go

The class declares:

- `folder = "{author}/{id} {title}/{part}"`: `author` is the post's category name (`misc` when it
  has none), `title` its title, and `part` is `images` for the previews and empty otherwise.
- `filename = "{id}"`, which is never used: every file is named by the post. Previews are `01.jpg`,
  `02.jpg` and so on (keeping the URL's extension), and archives keep their URL's file name.
- `key = ""`: the history key is the file's path, as `loi:<path>`.

Files go under `.flnd/media/loi/<id>/`:

```
.flnd/media/loi/1234567890/Example Author/1234567890 Example Title/info.md
.flnd/media/loi/1234567890/Example Author/1234567890 Example Title/images/01.jpg
.flnd/media/loi/1234567890/Example Author/1234567890 Example Title/example_archive.zip
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
  a 429 code or words like "rate" and "too many" as a rate limit, and an error with words like
  "quota", "limit" or "exceeded" as the daily quota. Handled since 2026-11-15.
- **Quota before time.** `archive` checks `--time` before fetching links, so a post outside the
  range doesn't use quota. Handled since 2026-11-12.

## Limits

- One post per target: there is no author, tag or search target. Any other link is refused with
  the forms above; the source declares no `unsupported` links.
- Archives need a VIP level at least the post's own. A post the account can't open gives no
  links, with a warning naming the level needed.
- The daily count is kept by this machine's calendar day, which may not be the day the site
  resets its quota.
- A post is counted as soon as its links are fetched, even if its archives then fail to
  download.
- Baidu links are written to `info.md` only; flnd doesn't download from Baidu.
