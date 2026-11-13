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
your disk, **fast, complete and dated**, and keeps it up to date. It also **finds** things on a
site (by genre, author, search) and hands them to the downloader **[7.10]**.

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
flnd download <targets…> [--from CATEGORIES] [--time SINCE,UNTIL] [--shrink [off]] [-v]
flnd sync [targets…] [-v]
flnd find <site> [words…] [key=value…] [--time SINCE,UNTIL] [-v]        [7.10]
flnd target ls                                                           [7.10]
flnd target forget <targets…> | all                                      [7.10]
flnd auth login                                                          [7.10]
flnd auth logout <sites…> | all                                          [7.10]
flnd auth ls                                                             [7.10]
```

Two groups keep the top level to the everyday commands (user, 2026-11-13): `target` for the
targets flnd keeps, `auth` for logins. `flnd login` and `flnd logout` move into `auth`, with no
alias left behind (one spelling).

`python -m flnd` is the same program. `flnd --help` and `flnd <command> --help` list every option
and every target form.

### `download`

Downloads each target: every category of it, or those `--from` names.

| Option | Meaning |
| --- | --- |
| `<targets…>` | `site:value` pairs or URLs (§3). With none, read from stdin, one per line; blank lines and `#` comments skipped. |
| `--from CATEGORIES` | The categories to take, as a comma list or repeated: `--from reels,tagged`. Without it, every category. Refused with a single post's link, which is downloaded whole. Not saved. |
| `--time SINCE,UNTIL` | Only posts dated from one day to another, both included: `2026-01-01,2026-06-30`; a side left empty (or `*`, quoted) has no limit. An undated post is kept. Not saved. |
| `--shrink`, `--shrink off` | Compress each photo, video and audio file to a balanced quality in its own format, keeping the result only when smaller; **saved for the target** and used by every later `download` and `sync` of it ([shrink](shrink.md)). |
| `-v`, `--verbose` | The status lines, and every line of progress and log as well (§11). |

A `download` that turns `--shrink` on shrinks the target's files already saved too, so a folder
is never half shrunk. The run's first line names a target's saved settings:
`ins:example_user: --shrink (saved)`.

### `sync`

Brings every target downloaded so far up to date, or only the targets given. It has one option,
`-v`, and **changes no setting**: to change how a target is downloaded, run `download` with the
option. For each target, in order (§7):
1. **repair**: files missing, empty, broken or undated are fixed;
2. **fetch**: posts new since the last run, with the target's saved settings and every category;
3. a one-line summary per target, then the totals.

### `find` [7.10]

Takes a query instead of a target: any words and filters its site's finder understands, and finds
things (users, pages, manga, posts), each with the target `download` takes. It saves what its
finder says to save for each result (a picture, a cover), the way `download` saves files, and
lists the results (§9). It is for finding; a found thing's own content is `download`'s job.

```
flnd find fbk example words kind=pages                  # pages named like that: a table, pictures, results.json
flnd find loi author=example_creator --time 2026-01-01,
flnd find mng genre=action | flnd download              # in a pipe: one target per line
flnd find --help                                        # every site's finder and its filters
```

- **The query is free**: any words, then any `key=value` filters; what they mean is the finder's
  to say. A filter its finder doesn't list is refused before any request, naming the ones it takes.
- **On a terminal**: a table of the main fields. **Saved**: each result's files, named by the
  finder's formats (by default under `media/<site>/find/<query>/`), and `results.json` with every
  field and target (§10).
- **In a pipe**: one target per line, so `download` reads them from stdin.
- Its files go in history like any download's, so the same query run again saves only new ones;
  it writes no setting, counts no quota, and isn't part of `sync`.

### `target ls` and `target forget` [7.10]

A command group for the targets flnd keeps (user, 2026-11-13), built on `shared.cli`'s groups
(`App.group`); `flnd target --help` lists its commands.

- **`flnd target ls`** takes nothing, as `auth ls` (user, 2026-11-13), and prints every target
  downloaded so far, one row each: its saved settings (`--shrink`), files and size on disk, last
  sync, and per category whether its listing is complete, interrupted (a saved cursor) or never
  finished. It reads `metadata/` and the folder sizes, and asks no site.
- **`flnd target forget <targets…>`** or **`flnd target forget all`** stops keeping targets, as
  `auth logout` takes sites or `all`: their saved settings, listing records and cursors go, so
  `sync` no longer touches them. **Their files and history rows stay**, so a later `download`
  skips what is saved. It names each target it forgot, and one it didn't know; with no target it
  refuses and lists the ones it keeps.

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

A target is `<site>:<value>` or a URL, where `<site>` is the site's code; `ins:example_user`, `ins:@example_user` and
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

- **A single post's link** (`instagram.com/p/…`, `x.com/…/status/…`, a TikTok video) downloads
  that post whole, into its profile's folder; `--from` is refused with it.
- **Unsupported pages** of a site (`x.com/home`, a Facebook group) are refused before anything
  runs, with the reason.
- **`profile`** is the target's picture and cover, always in `profile/`, never re-downloaded.

---

## 4. Sites: `Site`, `Downloader`, `Finder`

One module per site, in `flnd/sites/<code>.py` (was `flnd/sources/`). It holds the **site's
knowledge**: how to read its pages and APIs, its quirks, its login wall and blocks. It declares up
to three classes, **kept apart** (user, 2026-11-13), each matching what the user does with it:

```
Site          the site's access: login, cookies, domains, pace, signing, browser   ← auth
 ├─ Downloader  targets → categories → posts → files                             ← download, sync
 └─ Finder      a free query → found things                                       ← find
```

`Downloader` and `Finder` each name their `Site`, and neither knows the other. A site has a
`Site` and a `Downloader`, a `Finder`, or both. They share only the `Site` and the module's own
helper functions (how to read the site's JSON, say). The site helpers more than one site uses live
in `flnd/sites/common/` (`replay` feed reading, `relay`, `dash`, `watch`).

### Public modules

| Module | What a site module imports from it |
| --- | --- |
| `flnd.site` | `Site`, `FetchError`, `LoginError`, `Page`, `pages` |
| `flnd.downloader` | `Downloader`, `target`, `Post`, `File`, `Range`, `day`, `fill` |
| `flnd.finder` | `Finder`, `Query`, `Found`, `QueryError` (and `File`, `Range` from `flnd.downloader`) |

A site module imports only these, `flnd.sites.common`, `shared` and the standard library.
`flnd/sites/__init__.py` collects `SITES`, `DOWNLOADERS` and `FINDERS` by code. Today's `Source`
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
  every `Downloader` and `Finder` method. Its members: `cookies`; `fetch(url, …)` (paced, signed,
  with the site's back-off; a `FetchError` on failure; `in_page=True` sends it from the browser
  page); `goto(url)` and `page` (the browser tab, opened on first use); `handoff(until=, message=)`
  (a captcha or login a person solves in the visible browser); `client`; `state` (the site's own
  saved values); `secrets`; `info`, `warn`; `back_off(reason, retry_after=)`; and `pages(fetch,
  cursor)` for cursor pagination. Nothing in it is about downloading or finding.
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
```

| Part | What it is |
| --- | --- |
| **route** (`@target`) | A target's patterns and the method that reads it. A route with categories returns a context every category receives; one without yields its posts itself (a single post). |
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

### `Finder`: the find API

Its own class, one method, a free query; §9 has the whole of it.

### Per site, after Phase 7

| Site | Listing | Dates | Notes |
| --- | --- | --- | --- |
| ins | the profile's own GraphQL and `/api/v1/` requests, replayed from the page; the browser as fallback | `taken_at`, a grid post's `pk` | budgets 275 GraphQL / 10 min, 75 `/api/v1/` / 11 min; floor 0.5 s [7.8]; story music saved [7.3] |
| fbk | the Comet GraphQL the page embeds and requests, replayed | the node's or its parent's stamp; else the photo's own page [7.5] | floor 0.8 s [7.8]; listing memory removes its dated/undated duplicates [7.4] |
| tik | the browser (TikTok's WAF refuses flnd's own client; the direct API path is deleted [lean]) | `createTime`, else the id | every CDN address as a mirror [7.3] |
| twt | X's GraphQL timelines, then `SearchTimeline` past their cap [7.3] | `created_at` | X's rate headers; floor 0.2 s [7.8]; quoted tweets' media and card images saved [7.3] |
| tlg | Telethon | the message's date | resumes, several connections a big file, `cryptg` [7.8] |
| loi | its signed JSON API | the post's date | archives count against the day's VIP quota, so only `download --from archive` asks for them |

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

## 9. Finding [7.10]

### One engine, two inputs

The user's rule (2026-11-13): **a `Downloader` takes a target and does everything for it; a
`Finder` takes a query and finds things.** Facebook's downloader takes a user and downloads all of
them; Facebook's finder takes a string and finds users, each with a target `download` would take.
That input is the whole difference: below it, both run on **the same engine** (the job), and a
finder decides how its files are saved exactly as a downloader does.

```python
# flnd/sites/fbk.py: one site module; its helpers serve every class in it
class Facebook(Site): ...                       # the access (§4)
class FacebookDownloader(Downloader): ...       # a target in: a user, everything of theirs

class FacebookFinder(Finder):                   # a query in: users, pages, groups that match it
    site = Facebook
    filters = {"kind": "people, pages or groups", "city": "a city's name"}
    folder, filename, key = "find/{query}", "{title}_{id}", "{id}"

    def find(self, site, query: Query) -> Iterator[Found]:
        """Profiles and pages whose name matches the words, as Facebook's own search ranks them."""
        for item in search_results(site, query.text, kind=query.filters.get("kind", "people")):
            yield Found(item["id"], title=item["name"], target=f"fbk:{item['id']}",
                        kind=item["kind"], followers=item.get("followers"),
                        files=[File(item["picture"], name="picture")])
```

| | `Downloader` | `Finder` |
| --- | --- | --- |
| **takes** | a **target**, matched by `@target` patterns | a **`Query`**: `text` (any words), `filters` (any `key=value`), `time` |
| **shape** | routes, then categories, with a context between them | **one method**, `find(site, query)` |
| **yields** | `Post(id, files, date=, **fields)` | `Found(id, title=, target=None, files=(), date=None, **fields)` |
| **saves** | its `folder`, `filename`, `key` formats | its own `folder`, `filename`, `key` formats (default `find/{query}`, `{title}_{id}`, `{id}`) |
| **engine** | the job: names, dedupe by key, the downloader (§8), mtime, history | **the same job** |
| **on top** | listing memory, dates, links, `--shrink`, `sync`, `targets.db` | a table of results, `results.json`, targets in a pipe |
| **command** | `download`, `sync` | `find` |

- **A query is free.** `flnd find fbk example words kind=pages` gives the finder
  `Query(text="example words", filters={"kind": "pages"}, time=Range())`. What words and filters
  mean, and how they map to the site's search, is the finder's. `filters` lists the keys it takes,
  for `flnd find --help` and to refuse an unknown key before any request; a value it can't use
  raises `QueryError("kind takes people, pages or groups")`, shown as a usage error. An empty query
  is the site's default listing, when the finder has one.
- **`Found`** is one result: its `id` (for its key), a `title`, the `target` that `download` takes
  (None for a thing that can't be downloaded), its `files` (a cover, a picture, a thumbnail, or more
  when the finder wants them: `File`, as in §4), a `date` when it has one (for `--time` and the
  files' modified time), and fields to show, in the finder's order.
- **The job runs it as it runs a category**: each `Found`'s files are named by the finder's
  formats (with `{query}`, the query made safe for a folder name), skipped when their key is saved,
  downloaded by `shared.http`, dated, and recorded in history under `<code>:find:<key>`, so the
  same query run again downloads only new files. `flnd/find.py` is the thin front: it builds the
  `Query`, draws the results table as they come, writes `results.json` (every field and target) in
  the query's folder when the finder ends or stops, and prints targets in a pipe.
- **What `find` doesn't have**, being no download of a target: no `--from`, no listing memory or
  dating rules, no links, no `--shrink`, no `targets.db` row, no part in `sync`.
- **It stops when you do**: a pipe closing (`| head -20`) or Ctrl-C closes the finder's generator,
  so no further page is asked for, and what was found is saved.
- **Imports**: `flnd.finder` takes `File` and `Range` from `flnd.downloader` and nothing else of
  it; `flnd.downloader` imports nothing of `flnd.finder`; both import `flnd.site` (§4). The job
  takes either's items through one internal interface, so neither API knows the other.
- **Which finders first** is the user's to say: loi's once its search page is checked by hand;
  then others where a site has a search worth having (Facebook's people and pages, Instagram's
  accounts and hashtags, X's search). A new site (a manga reader) is a new module with a `Site`,
  and a `Downloader`, a `Finder`, or both.

## 10. Storage: `.flnd/`

`.flnd/` is taken from the folder flnd runs in. Keep it private: it holds your logins.

```
.flnd/
  media/<site>/<target>/<category>/<files>        every saved file           [7.1, built]
  media/<site>/find/<query>/                      what a finder saves, results.json   [7.10]
  metadata/
    history.db       every saved file by key: path, size, mtime, date, marks
    targets.db       each target's saved settings (--shrink), complete listings, saved cursors
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

Every `download`, `sync` and `find` writes `.flnd/log/<date>_<time>_<command>.log`: every line `-v`
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
  `engine`, `job` (downloads), `find` (finding), `site` (the public `Site`, and its live members,
  which today's engine-side `site.py` holds), `downloader`, `finder`, `post`, `settings`, `repair`,
  `session`; `flnd/common/` the engine's helpers; `flnd/sites/` one module per site, and
  `flnd/sites/common/` what sites share.

---

## 15. Not in flnd

Config files; per-run rate limits or a speed cap; a scheduler or daemon (cron runs `flnd sync`);
a persistent download queue (an interrupted listing restarts from its cursor instead); post
filters beyond `--from` and `--time`; zip or exec postprocessors; metadata sidecars beyond loi's
`info.md`; a browser TLS fingerprint; headless browsing by default; torrents.

---

## 16. Phase 7, with finding

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
| **10** | **sites split cleanly: `Source` becomes `Site` + `Downloader`, `flnd/sources/` becomes `flnd/sites/`; then the find API (`Finder`, `Query`, `Found`, `flnd/find.py`), `flnd find`; `flnd target ls`/`forget`; `flnd auth login`/`logout`/`ls`; loi's finder first** | new |

Finding comes last: it builds on the black-box `shared` (7), the status/table output (9) and the
listing memory's common fields (4). Its first finders wait for the user's example pages.

## Still to settle while building

- **loi's first finder** waits for what loi's search page offers (words, author, category, tags,
  sort), checked by hand on the test account with one example page; whoever builds step 10 asks
  the user for it then.
