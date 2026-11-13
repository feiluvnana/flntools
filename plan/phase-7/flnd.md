# flnd: the complete spec

What flnd is, does and promises once Phase 7 is built. It gathers the Phase 7 files, the Phase 6
decisions still standing, and the code at `292e6e6`, in one place. Where this file and an older
plan disagree, this file holds; where a Phase 7 file says more on one part, that file has the
detail. Each part says which Phase 7 step builds what isn't there yet (**[7.n]**); a part with no
tag is how flnd works today.

Written and approved by the user on 2026-11-13. Examples use example handles only.

---

## 1. What flnd is

flnd saves what people post on six sites (Instagram, Facebook, TikTok, X, Telegram, LOIBUS) to
your disk, **fast, complete and dated**, and keeps it up to date. It also **downloads what a query
matches** on a site (by genre, author, search): a query is a target, such as `mht?genre:example` **[7.10]**.

It is a command-line program in Python, built on `shared`, flntools' own toolkit. `shared` is a
black box: flnd asks it for a fetch, a download, a browser page, a progress row, and doesn't think
about how they're done.

### Promises

1. **Complete.** Every file a target has, in every category asked for, at the best quality the
   site serves. A listing that fails part way is never recorded as complete.
2. **Once.** A file is saved once, however many times and under however many names the site
   lists it; a file found in a second category is linked there, not saved twice.
3. **Dated.** Every file has its date in its name and as its modified time.
4. **Fast.** Files download as fast as the CDNs allow; the sites themselves are asked as fast as
   they tolerate, and no faster.
5. **Safe.** Logins are encrypted on disk, never printed, never sent to a CDN, never in a log.
   flnd never writes outside `.flnd/`, and never a name a site could turn into a path out of it.
6. **Calm.** A run shows what it is doing at a glance; `-v` shows everything; every run leaves a
   plain log to audit.

---

## 2. Commands

```
flnd download <targets…> [--from CATEGORIES] [--time SINCE,UNTIL] [--shrink [off]] [--sync] [-v]
flnd sync [-v]                                                           [7.10]
flnd sync ls                                                             [7.10]
flnd sync toggle <targets…>                                              [7.10]
flnd auth login                                                          [7.10]
flnd auth logout <sites…> | all                                          [7.10]
flnd auth ls                                                             [7.10]
```

Two groups keep the top level to the everyday commands (user, 2026-11-13): `sync` for what flnd
keeps up to date, `auth` for logins. `flnd login` and `flnd logout` move into `auth`, with no
alias left behind (one spelling).

`python -m flnd` is the same program. `flnd --help` and `flnd <command> --help` list every option
and every target form.

### `download`

Downloads each target: every category of it, or those `--from` names.

| Option | Meaning |
| --- | --- |
| `<targets…>` | `site:value` pairs, URLs or query targets (§3). With none, read from stdin, one per line; blank lines and `#` comments skipped. |
| `--from CATEGORIES` | The categories to take, as a comma list or repeated: `--from reels,tagged`. Without it, every category. Refused with a single post's link, which is downloaded whole. Not saved. |
| `--time SINCE,UNTIL` | Only posts dated from one day to another, both included: `2026-01-01,2026-06-30`; a side left empty (or `*`, quoted) has no limit. An undated post is kept. Not saved. |
| `--shrink`, `--shrink off` | Compress each photo, video and audio file to a balanced quality in its own format, keeping the result only when smaller; **saved for the target** and used by every later `download` and `sync` of it ([shrink](shrink.md)). |
| `--sync` | Also keep these targets up to date: `flnd sync` updates them from now on [7.10]. Without it, a target is downloaded and known (`sync ls` lists it, sync off) but not synced. |
| `--limit N` | Caps how many results a query target downloads [7.10]. |
| `--dry` | Lists the targets it would download, and downloads none [7.10]. |
| `-v`, `--verbose` | The status lines, and every line of progress and log as well (§11). |

A query target is one token, `<site>?<query>`, in Lucene's syntax (§9). It works anywhere a target does:

```
flnd download 'mht?genre:(vanilla OR romance) -genre:example chapters:>5'
flnd download 'mht?author:"example creator"' --limit 20
flnd download --dry 'mht?example words'
```

`flnd download --help` lists each site's query parameters.

A `download` that turns `--shrink` on shrinks the target's files already saved too, so a folder
is never half shrunk. The run's first line names a target's saved settings:
`ins:example_user: --shrink (saved)`.

### `sync`, `sync ls` and `sync toggle` [7.10]

A command group for what flnd keeps up to date (user, 2026-11-13), replacing the `target` group
this spec had before. **Nothing is synced unless asked**: `download --sync` marks its targets.
`shared.cli` gains a group's default command, so `flnd sync` runs by itself and `flnd sync ls` is
its subcommand.

- **`flnd sync`** updates every target marked to sync, and nothing else. It has one option, `-v`,
  and **changes no setting**: to change how a target is downloaded, run `download` with the
  option. For each target, in order (§7):
  1. **repair**: files missing, empty, broken or undated are fixed;
  2. **fetch**: posts new since the last run, with the target's saved settings and every category;
  3. a one-line summary per target, then the totals.
  With nothing marked, it says so and exits 0.
- **`flnd sync ls`** takes nothing and lists **every target flnd knows**, synced or not: sync on or
  off, its saved settings (`--shrink`), files and size on disk, last sync, and per category whether
  its listing is complete, interrupted (a saved cursor) or never finished. It reads `metadata/` and
  the folder sizes, and asks no site.
- **`flnd sync toggle <targets…>`** flips each target between synced and not, and prints its new
  state. **Nothing is deleted**: settings, history and cursors stay, so turning a target on again
  resumes it where it was.
- **Stores**: a `targets.db` row gains `sync`. A row written before this, with no `sync` field,
  counts as **on**, as it was synced then; nothing is rewritten. A new target downloaded without
  `--sync` is off, and the run says once: `not synced; add --sync to keep it up to date`.
- A synced **query** is `download --sync '<site>?<query>'`, built in Phase 7 step 10.

### `auth login`, `auth logout` and `auth ls` [7.10]

- **`flnd auth login`** asks for everything, in a terminal: the site, then the way (from a
  browser's cookies, a cookie file, or the site's own: Telegram's QR code, phone or Telegram
  Web), then what that way needs. It refuses without a terminal.
- **`flnd auth logout ins tik`** or **`flnd auth logout all`** removes saved logins: cookies,
  secrets and an API key.
- **`flnd auth ls`** prints each site with its login state (logged in, API key only, not logged
  in), the way it was made (Chrome, a file, QR), and a login cookie that has expired or will within
  24 hours, by name, never its value. It asks no site.

### Exit status

0 when every target ended well; 1 when a target failed, so a script can tell. A rate-limit wait,
a skipped file, or one file failing while its target went on is not a failed target. A usage
mistake prints the help's error line and exits 2.

---

## 3. Targets

A target is `<site>:<value>`, a URL, or a query `<site>?<query>` (§9), where `<site>` is the site's code; `ins:example_user`, `ins:@example_user` and
`https://www.instagram.com/example_user/` are the same target. Each downloader declares its forms
as patterns (`@target`), and `flnd --help` lists them, generated from those patterns.

| Site | Code | Targets | Categories (all by default) |
| --- | --- | --- | --- |
| Instagram | `ins` | a profile; a post or reel link | posts, reels, tagged, reposts, stories, highlights, profile |
| Facebook | `fbk` | a profile or page | posts, photos, tagged, albums, reels, videos, stories, profile |
| TikTok | `tik` | a profile; a video or photo post | videos, stories, playlists, reposts, liked, favorites, profile |
| X | `twt` | a profile; a post link | posts, media, highlights, reposts, replies, profile |
| Telegram | `tlg` | a chat: `@username`, title or id | photo, video, file |
| LOIBUS | `loi` | a post id or link | photo, archive |
| mimihentai | `mht` [7.11] | a manga id or link; a chapter link | chapters, cover |

- **A query** (`<site>?<query>`, §9) downloads each result its query route yields; `mht?example words` is one. A site without query routes refuses it.
- **A single post's link** (`instagram.com/p/…`, `x.com/…/status/…`, a TikTok video) downloads
  that post whole, into its profile's folder; `--from` is refused with it.
- **Unsupported pages** of a site (`x.com/home`, a Facebook group) are refused before anything
  runs, with the reason.
- **`profile`** is the target's picture and cover, always in `profile/`, never re-downloaded.

---

## 4. Sites: `Site` and `Downloader`

One module per site, in `flnd/sites/<code>.py` (was `flnd/sources/`). It holds the **site's
knowledge**: how to read its pages and APIs, its quirks, its login wall and blocks. It declares up
to two classes, **kept apart** (user, 2026-11-13), each matching what the user does with it:

```
Site          the site's access: login, cookies, domains, pace, signing, browser   ← auth
 └─ Downloader  routes: targets → categories → posts → files; or targets, run as targets   ← download, sync
```

A site has a `Site` and a `Downloader`, which names its `Site`. They share only the `Site` and the module's own
helper functions (how to read the site's JSON, say). The site helpers more than one site uses live
in `flnd/sites/common/` (`replay` feed reading, `relay`, `dash`, `watch`).

### Public modules

| Module | What a site module imports from it |
| --- | --- |
| `flnd.site` | `Site`, `FetchError`, `LoginError`, `Page`, `pages` |
| `flnd.downloader` | `Downloader`, `target`, `Post`, `File`, `Range`, `day`, `fill` |
| `flnd.query` | `Param`, the kinds (`Text`, `Name`, `Number`, `Date`, `Choice`), `Query`, `Term`, `Value`, `Lookup`, `QueryError` |

A site module imports only these, `flnd.sites.common`, `shared` and the standard library.
`flnd/sites/__init__.py` collects `SITES` and `DOWNLOADERS` by code. Today's `Source`
splits into `Site` and `Downloader`, today's `flnd.source` into `flnd.site` and
`flnd.downloader`, and `sources.db` becomes `sites.db`, with no alias left behind (one spelling)
[7.10]. "Source" leaves flnd's vocabulary: the word is **site** everywhere, in code, messages and
docs.

### `Site`: the access

```python
class Example(Site):
    code, name, domains = "exm", "Example", ("example.com",)
    needs = ("sessionid",)                      # cookies a login must have
    pace = 0.5                                  # least seconds between two requests to the site

    def prepare(self, request): ...             # optional: sign each request (loi)
```

- **Fields**: `code`, `name`, `domains`, `needs`, `requires_login`, `login_methods`, `pace`,
  `user_agent`, `stealth`; **hooks**: `prepare` (sign or adjust each request), and the site's own
  login when it has one (Telegram's QR, phone, Telegram Web).
- **Live**: the engine makes one `Site` object per site for a run, and passes it as `site` to
  every `Downloader` method. Its members: `cookies`; `fetch(url, …)` (paced, signed,
  with the site's back-off; a `FetchError` on failure; `in_page=True` sends it from the browser
  page); `goto(url)` and `page` (the browser tab, opened on first use); `handoff(until=, message=)`
  (a captcha or login a person solves in the visible browser); `client`; `state` (the site's own
  saved values); `secrets`; `info`, `warn`; `back_off(reason, retry_after=)`; and `pages(fetch,
  cursor)` for cursor pagination. Nothing in it is about downloading.
- `auth login`, `logout` and `ls` work on `Site`s alone.

### `Downloader`: the download API

```python
class ExampleDownloader(Downloader):
    site = Example                              # whose access it uses
    folder, filename, key = "{category}", "{date}_{id}{index}", "{id}_{num}"

    @target("{user}", "example.com/{user}")     # a route: a target in, a context out
    def user(self, site, user): return profile_of(site, user)

    @user.category(newest_first=True)           # a category: posts out
    def posts(self, site, profile): yield from posts_of(site, profile)

    def date(self, site, post): ...             # optional: one item's date, by its own page [7.5]
    @target("?{query}", params={"tag": Param(Name, "a tag", lookup=tags)})   # a query route [7.10]
    def tagged(self, site, query: Query) -> Iterator[str]:
        for item in example_search(site, query):
            yield f"exm:{item['id']}"
```

| Part | What it is |
| --- | --- |
| **route** (`@target`) | A target's patterns and the method that reads it, of one of three kinds: **a context** that its categories receive (a user, a manga); **posts** it yields itself (a single post, a chapter); or **targets** it yields, which the engine runs as ordinary targets (an author page, a genre page, a query: `@target("?{query}", params={...})`, whose method gets a `Query`). |
| **category** (`@<route>.category`) | One thing a target can download, a generator of `Post`s. `newest_first` lets the engine stop early (§6). Its own `folder` and `key` may differ from the downloader's. |
| **`date` hook** [7.5] | Asked for a post still undated when its category ends: one paced request for that item's own date. |
| **fields** | `site`, `unsupported` (pages of the site that aren't targets), `folder`, `filename`, `key`. |
| **per target** | The downloader's instance holds `self.target` and `self.time` (`--time`), which only the download side has. |

- **`Post(id, files, date=, headers=, **fields)`**: one post. `files` is a tuple, or a function
  (a **lazy** post) the engine calls only when a file is needed, so a saved post costs no request.
  `fields` feed the name formats; every site uses the same keys where it has them: `title`,
  `author`, `category`, `tags`, `url`.
- **`File(url, name=, audio=, size=, headers=)`**: one file. `url` is one URL, a tuple of mirrors
  tried in order (every alternate CDN address, best first **[7.3]**), or a function that fetches the
  file itself (Telegram). `audio` is a second stream merged into a video (DASH). `File.text(name,
  content)` is a text file the downloader writes (loi's `info.md`).

### Per site, after Phase 7

| Site | Listing | Dates | Notes |
| --- | --- | --- | --- |
| ins | the profile's own GraphQL and `/api/v1/` requests, replayed from the page; the browser as fallback | `taken_at`, a grid post's `pk` | budgets 275 GraphQL / 10 min, 75 `/api/v1/` / 11 min; floor 0.5 s [7.8]; story music saved [7.3] |
| fbk | the Comet GraphQL the page embeds and requests, replayed | the node's or its parent's stamp; else the photo's own page [7.5] | floor 0.8 s [7.8]; listing memory removes its dated/undated duplicates [7.4] |
| tik | the browser (TikTok's WAF refuses flnd's own client; the direct API path is deleted [lean]) | `createTime`, else the id | every CDN address as a mirror [7.3] |
| twt | X's GraphQL timelines, then `SearchTimeline` past their cap [7.3] | `created_at` | X's rate headers; floor 0.2 s [7.8]; quoted tweets' media and card images saved [7.3] |
| tlg | Telethon | the message's date | resumes, several connections a big file, `cryptg` [7.8] |
| loi | its signed JSON API | the post's date | archives count against the day's VIP quota, so only `download --from archive` asks for them |
| mht [7.11] | its public JSON API: `/api/manga/{id}`, `/api/manga/{id}/chapters`, `/api/chapters/{id}` | a chapter's `created_at` | no login needed; floor 0.5 s, learned; a query route over its advanced search (§17) |

---

## 5. One run, step by step

```
targets ─► resolve (site, route, value) ─► group by site, sites side by side            [7.8, lean E5]
  per target:  route(site, value) ─► for each wanted category, in declaration order:
      for post in category:
          date it from the listing memory; drop a second copy of a key            [7.4]
          --time filter; newest_first early stop; sync's saved-run stop (§7)
          hold an undated post until its category ends, then date it              [7.4, 7.5]
          files: listed now, or (lazy) only if the post isn't saved
          for each file: name and key from the formats
              saved here ─► skip     saved in another category ─► link (§6)       [7.6]
              else ─► queue the download ─► shared.http (§10)
              then ─► shrink (if on) ─► modified time ─► history row (last)
  ─► status line per target and category ─► totals ─► exit status
```

- **Names.** `folder`, `filename` and `key` are format strings over a post's fields and the file's
  (`{category}`, `{date}`, `{id}`, `{index}`, `{num}`, `{title}`, `{author}`, …). Every value is made
  safe for a file name (`..` becomes `untitled`); the extension comes from the file's content, not
  its URL. A file keyed by id is named from the format, so a date found later reaches its name
  [7.4].
- **History.** One row per file, keyed `<site>:<key>`, written last, after the file is in place:
  its path (relative to `media/`), size, modified time after postprocessing, date, and marks
  (`estimated`, `compressed: "mid"`, `files: n` on a carousel's first file [7.3]).
- **Errors.** A failed file is logged with its cause and the target goes on. A route or category
  that raises fails its target: queued downloads are dropped, running ones finish. A listing cut
  short raises, so it is never recorded complete [7.3].
- **Ctrl-C.** The first stops listing, lets running downloads finish, and ends targets as
  stopped; the second stops downloads at once, keeping their resumable parts [7.7: within a
  second].

---

## 6. Once, dated, linked

### Each file once [7.4]

The job keeps, for the whole target, **each key once** and **one key → date map**, filled from
the listing and from history. A post listed twice (Facebook's feed sends a photo inside its dated
post and alone, undated) yields one file under its dated name, whichever copy came first. A file
already saved, by path or by its history row, is skipped; at info each category says once how many
(`fbk:example.page: photos: 131 files already saved`), and `-v` names each skip and the file that
holds it.

### Every file dated [7.5]

A post's date comes from the listing. One still undated when its category ends is dated, in
order, by: another copy's date in the map; the downloader's `date` hook (Facebook: the photo's own
page, one paced request); else the date of the nearest dated post in listing order, marked
`estimated` in its row. A block while dating estimates the rest without failing the run. The date
goes in the name (`2026-11-11_<id>.jpg`) and is the file's modified time (UTC day); a file with no
date at all falls back to the CDN's `Last-Modified`, then the download day.

### Linked, not copied [7.6]

A file already saved in **another category's folder** (a reel in `videos/` and `reels/`, a photo in
`posts/` and `photos/`, an X post in `posts/` and `media/`) gets a **relative symlink** at the path
it would have had; where symlinks can't be made (Windows without the right), a hard link; where
neither can, a skip with one warning per run. A link has no history row; `sync` remakes a missing
one and removes one whose file is gone after restoring it.

---

## 7. `sync`

1. **Repair** [7.3, 7.8]. For each history row of the target:
   - a file whose size and modified time match its row is trusted; one that changed is fully
     checked (a photo decoded, a video's packets read), and so is a rolling 1/30 of the rest each
     sync, so silent corruption is found within a month; checks run on a thread pool, alongside the
     listing;
   - a missing, empty or broken file is downloaded again; a leftover `.part` is removed;
   - a file saved **undated or with an estimated date** is dated through the downloader's `date` hook
     and renamed, its row and modified time updated [7.5].
2. **Fetch.** Every category, newest first where it can. A `newest_first` category whose last
   listing reached its end stops after **5 saved posts in a row**: sync has caught up. A queued
   file counts neither way [7.3]. A target with a file to restore lists **to the end** in that sync,
   so a lost file deep in the feed is reached [7.3]. A listing interrupted earlier **restarts from
   its saved cursor**, then lists the newest posts down to where it began [7.3].
3. **Summary.** One line per target (new, repaired, missing, linked, failed), then totals.

---

## 8. Downloads

### The downloader [7.8, in `shared.http`]

- **Bounded pieces, IDM's way.** A file of 4 MB or more, from a host that takes ranges, is cut into
  2–4 MB pieces; its connections (up to 8) pull the next piece from one queue, in order. Every
  stream ends at its piece's end, so connections stay alive for the next piece and file.
- **Stalls** (MDM's rules): no first byte after twice the file's first time-to-first-byte gives the
  piece away; after 2 s, a connection slower than a quarter of the best speed any connection of the
  file sustained reconnects. Never while the disk is the bottleneck.
- **Connections per host** start at 4 and grow while throughput grows, up to 8; a host's cap is set
  by two refusals while others receive, and forgotten after 10 minutes.
- **HTTP/2** for photos and small files (one connection per CDN); a split file over HTTP/1.1.
- **Resume** from a `.part` and its `.part.json`, with `Range` and `If-Range` against the server's
  `ETag` or `Last-Modified`; without a validator, after its last 64 bytes fetched again match.
- **Checks**: the size; the server's hash when it sends one; the media check (a web page or JSON
  saved as a photo is refused); free space before a big file.
- **Retries**: a dropped connection once at once, then 1 s doubling to 30 s; a CDN's `Retry-After`
  up to 300 s. An expired signed link (403/410) asks the post's lazy `files` once for a fresh one.
- **Stoppable**: `download(…, stop=)`, within a second, keeping the part [7.7].

### At once

8 files per target, videos first; several targets at once, one per site, each site at its own
pace. DASH video and audio download together and are merged by ffmpeg; Telegram fetches a big
file's parts on several connections and resumes.

### The sites' own pace

Site requests (not CDN downloads) wait for the latest of: the site's rate headers (5 left → wait
for the reset); each budget's sliding window; a learned gap of at least the site's floor plus up
to 40% jitter. The learned speed doubles on a throttle (up to 8×), eases after 50 clean requests,
and is saved per site. The site and its CDNs have **separate back-offs**, so a CDN 429 never
slows the listing [7.8]. A block or a login wall is waited out with a countdown, or handed to a
person in the browser.

| ins | fbk | tik | twt | tlg | loi |
| --- | --- | --- | --- | --- | --- |
| 0.5 s + budgets | 0.8 s | 1.0 s | 0.2 s + headers | flood waits | 0.5 s |

### `--shrink` [built, 7.8]

Each saved photo, video and audio file is re-encoded at `media`'s `mid` level **in its own
format**, metadata (GPS too) stripped, and replaces the original only when smaller. A video already
at or below the level's bitrate isn't encoded; for the rest, a 4 s slice is tried first [7.8].
Videos encode one at a time across the run; photos on a pool of 8 [7.8].

---

## 9. Query targets [7.10]

### The decision

The user's decisions (2026-11-13): "So should only have download. Now download can choose a query target. Remove finder and call it a day." and "delete finder, make everything downloader, downloader can have multiple kind of target". So there is one command and one class per site. A query is a route of the site's `Downloader`, and `download` takes `<site>?<query>` wherever it takes a target: arguments, stdin lines and `download --sync`.

### A query route

```python
# flnd/sites/mht.py: one site module; its helpers serve every class in it
class Mimi(Site): ...                           # the access (§4)
class MimiDownloader(Downloader):
    site = Mimi

    @target("?{query}", params={                # a query route: a Query in, targets out
        "title":     Param(Text, "words in the title (bare words mean this too)"),
        "genre":     Param(Name, "a genre", any_of=False, negate=True, lookup=genres),
        "author":    Param(Name, "an author", lookup=authors),
        "character": Param(Name, "a character", lookup=characters),
        "parody":    Param(Name, "the work it parodies", lookup=parodies),
        "chapters":  Param(Number, "how many chapters", compare=False),
    })
    def query(self, site, query: Query) -> Iterator[str]:
        """Yields the manga that match a query, as targets."""
        for item in advanced_search(site, query):           # query.terms → the site's parameters
            yield f"mht:{item['id']}"
```

- **The query** is one token, `<site>?<query>`, in Lucene's syntax (below). Quote it in a shell: it has spaces and brackets. A parameter the route doesn't declare, or a value of the wrong kind, is refused before any request, with where in the query and what that site takes.
- **A query route** gets a plain `Query` (below), once per search, and yields targets such as `mht:70982`.
- **Target routes** (the third kind in §4) all share these rules. The engine runs each yielded target as an ordinary target: the same job, history, dates, `--shrink` and links. Results are deduped by target. `download --limit N` caps how many a route downloads, and `download --dry` lists the targets it would download, downloading none.
- **Synced**: `download --sync '<site>?<query>'` is one row in `sync ls`. Each `flnd sync` runs the route again and downloads only results it hasn't seen, at most 25 new per query per run. Targets the user already follows are left alone. Seen results are kept in `targets.db` (§10).
- **Help**: `flnd download --help` lists each site's query parameters.
- **It stops when you do**: reaching `--limit` or Ctrl-C closes the route's generator, so no further page is asked for, and what was downloaded is kept.

### The query language: Lucene's, parsed by luqum

flnd invents neither a syntax nor a parser (user, 2026-11-13). A query is written in **Lucene's
query syntax** (what Elasticsearch, Kibana and many search boxes take) and parsed by
**[luqum](https://github.com/jurismarches/luqum)** (1.0.0, Apache-2.0, needs only `ply`; checked
on Python 3.14 on 2026-11-13):

| Lucene | Means |
| --- | --- |
| `example words`, `"a phrase"` | free text: the site's own search, or its `title` param |
| `param:value`, `param:"two words"` | a parameter |
| `param:(a OR b)`, `param:(a AND b)` | any of, all of |
| `-param:value`, `NOT param:value` | not |
| `a OR b`, `a AND b`, `( … )` | combine; side by side means AND |
| `param:[3 TO 8]`, `param:[3 TO *]`, `param:>5`, `param:<=10` | ranges and comparisons |
| `param:[2026-01-01 TO 2026-06-30]` | dates |

- **Each query route declares its own parameters** (user, 2026-11-13), as its `params`: a
  name, a kind (`Text`, `Name`, `Number`, `Date`, `Choice(...)`), a one-line help, and what the
  site can do with it (`any_of`, `negate`, `compare`), and for a name the site knows by id, a
  `lookup` that turns names into ids. Nothing is shared between sites but the syntax: Facebook's
  `kind`, mimihentai's `genre`, X's `from` are each that site's own. `flnd download --help` lists
  them; `sort` is a parameter too, where a site declares it.
- **`flnd/query.py`** is the only place that touches luqum: it parses the query (luqum's own
  error, with its position, is the usage error), walks the tree with luqum's `TreeVisitor`, checks
  every field against the route's `params` (unknown name, wrong kind), splits an OR the site can't
  send into several searches (below), turns names into ids through the lookups, and hands the
  query route, once per search, a plain **`Query`**: `text` (the free words), `terms` (each `Term(param,
  values, negated, low, high)`), and `time` (`--time`). A downloader never sees luqum's tree.
- **OR on a site without it runs as several searches, merged** (user, 2026-11-13). Most sites
  take AND but not OR. `flnd/query.py` rewrites the query into an OR of AND-groups (De Morgan
  first, so `-(a OR b)` is `-a -b`), and each AND-group the site can send becomes **one search**:

  ```
  genre:(vanilla OR romance) -genre:example     →  genre:vanilla -genre:example
                                                    genre:romance -genre:example
  (parody:example OR character:example) chapters:1  →  parody:example chapters:1
                                                        character:example chapters:1
  ```

  The searches run one after another; results are **merged and deduped by target**, in the order
  they first arrive, so a manga matching both genres is listed once. `--limit` counts the merged
  results, and reaching it stops every search. A site that declares `any_of` on a parameter (its
  own OR within one parameter) gets that OR sent whole, as one search. More than **8 searches** is
  refused with how many the query would need, so a query never fans out unnoticed. Each search is
  paced as any request is. This split applies to query routes only.
- **What no number of searches can do is refused, not guessed**: a comparison on a parameter
  declared `compare=False`, a negation the site can't send, a parameter it doesn't take. The message
  names the part of the query and what that site takes. No query is silently widened or narrowed.
- **Lookups** (a genre's or an author's id by name) are asked once per run and kept in
  `sites.db` for a day; names match ignoring case and accents; a name that matches several stops
  with the candidates.
- luqum builds its parsing tables on first import and `ply` prints warnings then; `flnd/query.py`
  builds them quietly (ply's error log silenced) and once per run. luqum goes in `pyproject.toml`'s
  dependencies.
- **Which sites first**: loi's query targets, then others where a site has a search worth having
  (Facebook's people and pages, Instagram's accounts and hashtags, X's search). A new site (a manga
  reader) is a new module with a `Site` and a `Downloader`.

## 10. Storage: `.flnd/`

`.flnd/` is taken from the folder flnd runs in. Keep it private: it holds your logins.

```
.flnd/
  media/<site>/<target>/<category>/<files>        every saved file           [7.1, built]
  metadata/
    history.db       every saved file by key: path, size, mtime, date, marks
    targets.db       each target's saved settings (--shrink), complete listings, saved cursors, a synced query's seen results
    sites.db         each site's own state (learned pace, tokens, loi's quota), a table per site
    secrets.db       every login, encrypted (cookies, Telegram's session, API keys)
    secret.key       the key that opens secrets.db
  log/<date>_<time>_<command>.log                 one plain log per run, kept 30 days
```

- No migration: an old `.flnd/` (with `downloads/`) is left as it is; README says how to move it
  by hand.
- Files are written through a hidden `.<name>.<random>.part` and renamed into place; on Windows the
  rename is retried for a second when a scanner holds the file [7.7]. `sync` removes stray parts.
- A history row's path is relative to `media/`, so moving `.flnd/` as a whole keeps it working.

---

## 11. Output

### Without `-v` [7.9]

One live status line per running target and category, and a second line only while files run:

```
✓ fbk:example.page  posts    164 found   150 saved   12 skipped   2 failed   24.1 MB  1m 12s
⠹ fbk:example.page  photos   140 found     9 saved  131 skipped   3 linked   3.4 MB  8.2 MB/s
                             4 downloading  1 shrinking
```

A finished category leaves its line as its summary. Warnings and errors print in full above (a
failed file with why, a rate limit with its countdown, a refused login). Nothing prints per file.
A narrow terminal drops the words and keeps glyphs and numbers; nothing wraps. In a pipe or file:
no live area; each category's summary, warnings, errors and totals. `Complete. 159 files` ends it.

### With `-v`

The same status lines, and under them the files running now, and above them every finished file,
skip, link, shrink and the site's own debug lines:

```
✓ ━━━━━━━━━━━━━━━━━━━━ posts/2026-11-11_122168984774665018.jpg 45.4 KB      [built]
• ━━━━━━━━━━────────── videos/2026-08-20_2125341001354648.mp4 7.3 MB/14.6 MB
↷ posts/122168984888665018.jpg → posts/2026-11-11_122168984888665018.jpg
```

Every bar starts at one column and every name after it; the size follows its name. A row with
nothing to count (a target, a scroll) stays one short line [built].

### Colour and motion [7.9]

Pleasant, not flashy. Plain white only for what must be read (a file's name, a warning's
message); debug faint grey, info muted, sizes and times dim, the target bold. ok green, warn
yellow, error red. While a row runs: a soft two-shade gradient drifting along its bar and a spinner
through the accent's shades; waits steady yellow; counts never animate. A row that ends goes still
in one flat colour. No colour or motion for `NO_COLOR`, `TERM=dumb`, a pipe or a file; flat colour
on 8-colour terminals; ASCII glyphs where Unicode isn't safe.

### Run logs [7.1, built]

Every `download` and `sync` writes `.flnd/log/<date>_<time>_<command>.log`: every line `-v`
shows, at debug, with its time, in plain text, whatever the screen shows; each phase marked (start
with the options as given, per target resolve, repair, list, shrink, link, summary; totals and exit
status); warnings and errors with their cause. URLs never carry their query; no cookie, key,
password, session or `Authorization` value ever appears. Logs older than 30 days are deleted at
the start of a run.

---

## 12. Logins and secrets

- **Ways in**: a browser's cookies (Chrome, Edge, Brave, Firefox, Safari, with the profile asked
  when there are several), a `cookies.txt`/JSON file, or the site's own (Telegram: QR, phone and
  code, Telegram Web). Only the site's own cookies are kept.
- **Kept encrypted** in `metadata/secrets.db` with `metadata/secret.key`. Cookies the site renews
  are written back. A login cookie that has expired, or will within 24 hours, is a warning at the
  run's start, naming the cookie, never its value [7.3].
- **Never leaves its site**: requests to a CDN carry no cookie or `Authorization`. No message,
  log, exception or test holds a secret.
- **Browser**: Google Chrome, visible; when Chrome is missing, flnd offers to download Chromium.

---

## 13. flnd and `shared`

flnd uses `shared` as a black box: `http` (fetch, download), `browser` (pages, responses, in-page
fetch), `log` and `theme` (lines, rows, the live area, the run log), `cli` and `prompt` (commands,
questions), `kv` (stores), `atomic` (safe writes), `worker` (pools), `media` (checks, `--shrink`),
`cookies`, `crypto`, `archive`, `selector`, `fmt`.

Rules [7.7]: flnd never imports `shared._x`, never copies a `shared` constant or helper, never
reads a side channel for what a call should return, and catches `shared`'s errors by name. A
`shared` call returns what its caller needs (`download` its headers, `goto` its document), can be
stopped, and raises only `<module>.Error` types (`MissingError` for a missing program or browser).
`shared`'s public API is kept by usefulness, not by flnd's use; only aliases go.

---

## 14. Conventions and checks

- Names: a verb for an action, a noun for a read, a property for a read with no arguments, no
  `get_`/`to_` prefixes, options after the first parameter keyword-only, one word for one thing
  (`dest`, `timeout`, `progress(done, total)`, `label`, `level`), no aliases. `tests/test_names.py`
  enforces them.
- Every module, public class and function has a docstring (one sentence, an example, `Args:`,
  `Returns:`, `Raises:`); comments explain why, at every magic value, quirk, lock and branch.
- `uv run --no-project scripts/dev.py check`: lint, pyright (0/0) and the tests, green at every
  commit; `api` and `baseline` regenerate the snapshots. Tests fake the network, browser and
  clock; live checks are by hand on the test accounts, never in tests.
- Commits `<action>:<summary>`, signed, one logical change each; pushed only when asked.
- **Layout, after step 10** (AGENTS.md is updated in the same commit): `flnd/` keeps `cli`,
  `engine`, `job` (downloads), `site` (the public `Site`, and its live members,
  which today's engine-side `site.py` holds), `downloader`, `post`, `settings`, `repair`,
  `session`; `flnd/common/` the engine's helpers; `flnd/sites/` one module per site, and
  `flnd/sites/common/` what sites share.

---

## 15. Not in flnd

Config files; per-run rate limits or a speed cap; a scheduler or daemon (cron runs `flnd sync`);
a persistent download queue (an interrupted listing restarts from its cursor instead); post
filters beyond `--from` and `--time`; zip or exec postprocessors; metadata sidecars beyond loi's
`info.md`; a browser TLS fingerprint; headless browsing by default; torrents.

---

## 16. Phase 7

| Step | | State |
| --- | --- | --- |
| 0 | aligned progress columns, skips per category, `download -v` | built |
| 1 | `.flnd` as media, metadata and log; run logs | built |
| 2 | `--shrink` | built |
| 3 | fixes: 11 items | building |
| 4 | one listing memory | |
| 5 | every file dated | |
| 6 | links | |
| 7 | `shared` as a black box | |
| 8 | speed | |
| 9 | status lines, colour and motion | |
| **10** | **sites split cleanly: `Source` becomes `Site` + `Downloader`, `flnd/sources/` becomes `flnd/sites/`; then query targets (`<site>?<query>`, a query route of a `Downloader`; `Param`, `Query`) with Lucene queries parsed by luqum (`flnd/query.py`); the `sync` group (`flnd sync`, `sync ls`, `sync toggle`, `download --sync`); `flnd auth login`/`logout`/`ls`; loi's query targets first** | new |
| **11** | **mimihentai (`mht`)**: its `Site` and `Downloader` (a manga's chapters, and a query route over its advanced search), per §17 | new |

Query targets come last: they build on the black-box `shared` (7), the status lines (9) and the
listing memory's common fields (4). Loi's first query targets wait for the user's example pages.

## 17. mimihentai (`mht`) [7.11]

A Vietnamese manga site, mimihentai.moe: a Nuxt front end on Cloudflare, over a **public JSON
API on the same host** that needs no login. Read from its scripts and a few anonymous requests on
2026-11-13 (structure only).

### Its API

| What | Call | Gives |
| --- | --- | --- |
| a manga | `GET /api/manga/{id}` | `title`, `cover_url`, `description`, `authors[{id,name}]`, `genres[{id,name}]`, `alt_names`, `chapter_count`, `last_updated`, `view`, `follows` |
| its chapters | `GET /api/manga/{id}/chapters?limit=100` | `[{id, title, order, created_at, manga_id}]` |
| a chapter | `GET /api/chapters/{id}` | `info{id, title, order, created_at, manga_id}`, `pages[{image_url}]`, `prev`, `next` |
| advanced search | `GET /api/manga/advanced-search` | params `title`, `author` (ids, comma), `character`, `parody`, `genre` (ids, comma, all of), `exclude_genre` (ids), `chapters`; paged by `cursor` + `cursor_res=true` → `{items, next_cursor, total}` |
| simple search | `GET /api/manga/search?q=` | the same items |
| lookups | `/api/genres` (296), `/api/authors/search`, `/api/characters/search`, `/api/parodies/search` | names → ids |
| images | `https://moe-cdn.net/<x>/<file>.webp` | no referer needed; `Accept-Ranges`, `ETag`, `Last-Modified`; about 140 KB a page |

Checked on 2026-11-13, against the public API and the site's own search code:
- **Chapters aren't paged**: `/api/manga/{id}/chapters` returns every chapter whatever `limit`
  says (131 for a 131-chapter manga, with `limit=100`, `page=2` or `offset=100` alike).
- **`chapters` does nothing**: the advanced search ignores it in every form tried (`1`, `5`,
  `1-5`, `10-`), alone or with a genre. The query route filters by `chapter_count` itself, which each
  result carries, rather than send it.
- **`character` takes names**, free text joined by commas, as the site's form sends them; no
  lookup to ids.
- **`advanced-search` only with a structured filter** (author, character, parody, genre); a query
  of words alone goes to `/api/manga/search?q=`. Both page by `cursor` with `cursor_res=true`
  (24 items a page, `{items, next_cursor, total}`).
- **Rate**: about 20 requests at one a second drew no refusal; the ceiling wasn't probed. The
  0.5 s floor, learned upward, stays.

### Download

```
flnd download mht:70982
flnd download https://mimihentai.moe/manga/70982
flnd download https://mimihentai.moe/manga/70982/chapter/<chapter_id>      # that chapter only
flnd download mht:70982 --from chapters --time 2026-01-01,
```

- **Targets**: a manga by id or link; a chapter's link downloads that chapter.
- **Categories**: `chapters`, each chapter a lazy post dated by its `created_at`, whose pages are
  asked for only when it isn't saved; `cover`, the manga's cover, once. A `sync` costs one request
  per manga and one per new chapter.
- **Layout**: `media/mht/<id>/chapters/<order:03> <chapter title>/001.webp …`, and
  `media/mht/<id>/cover/cover.webp`; keyed by chapter id and page number, so a renamed chapter is
  never downloaded again.

### Query targets

mimihentai's routes (§4): a manga, a chapter, a query, and the author and genre pages if the site
has them (checked by hand, not yet known).

| Route | Kind | What it gives |
| --- | --- | --- |
| `mht:{id}`, `mimihentai.moe/manga/{id}` | a context | the manga, with categories `chapters` and `cover` |
| `mimihentai.moe/manga/{id}/chapter/{chapter_id}` | posts | that chapter's post |
| an author's page link, if the site has one | targets | manga, each as `mht:<id>` |
| a genre's page link, if the site has one | targets | manga, each as `mht:<id>` |
| `mht?{query}` | targets | manga that match the query, each as `mht:<id>` |

```
flnd download 'mht?genre:(vanilla AND romance) -genre:example'
flnd download 'mht?author:"example creator" chapters:1'
flnd download 'mht?example words'
```

The query route's `params` stay the same, as in §9: `title`, `genre` (all of, negatable, by name through `/api/genres`), `author`, `character`, `parody` (by name through their search APIs) and `chapters`. Bare words alone use the simple search; anything else, the advanced one. Each result it yields is downloaded as a manga target, with its chapters and cover as above.

## Still to settle while building

- **loi's query targets** (user, 2026-11-13): `tag:` and `author:` by name, looked up through the catalogue API, and words through the keyword search, each yielding `loi:<id>` posts. Listings of every tag or author are dropped, since they can't be downloaded. The API, read from loix.cc's own JavaScript on 2026-11-13 (signed, on `api.moegoat.com/api/`): every author (`人物库`, which the API calls loiers and categories) `user/library/loiers?page=N`; every tag in three groups (play 1, clothing 2, character 3) `user/library/tags?group_id=G&page=N`; an author's posts `user/library/loier/id?category_id=ID&sort=latest|popular|choicest&page=N`; a tag's posts `user/library/tag/id?tag_id=ID&sort=…&page=N`; a keyword search `user/loi/search/<keyword>/<latest|popular|choicest>?page=N`. Each answer is `{data: [...], pagination: {total}}`, paged until `data` is empty; a tag or author item has `id`, `name` (or `title`), `cover` and `count`, a post item what `lois` lists. The answers' exact fields are unverified: a plain client gets the site's bot wall (403), so a live run by hand confirms them.
