# flnd spec

Everything about flnd on one page: what each command takes, what each site's plugin reads, what each
refusal says. The words, once: a **target** is what you ask for
(`ins:example_user`), a **plugin** is the code for one site, a **route** is the part of a plugin
that reads one kind of target, a **category** is one part of a target (`reels`), a **post** is one
item of a site with its **files**, and the **home** is the folder where flnd keeps all of it.

Sections: [Install and first run](#install-and-first-run) · [Targets](#targets) ·
[Dates](#dates) · [Categories](#categories) · [Folders](#folders) · [Sync](#sync) ·
[Logins](#logins) · [When it goes wrong](#when-it-goes-wrong) · [Commands and
flags](#commands-and-flags) · [Config](#config) · [Plugins](#plugins) ·
[For developers](#for-developers)

## Install and first run

flnd needs [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer.

```sh
git clone https://github.com/feiluvnana/flntools
cd flntools
uv tool install -e ./flnd        # puts `flnd` on your PATH
```

From a checkout, `make setup` (inside `flnd/`) makes `.venv`, then `uv run flnd ...` runs it. On
Windows, `winget install ezwinports.make`, or type the `uv` command each target holds.

Outside programs, each needed only for some files:

- Google Chrome, for the sites that open a browser. When missing (and no Playwright Chromium
  either): the run stops with "Google Chrome is needed"; the hint says to install Chrome, or run
  `uv run playwright install chromium`.
- ffmpeg, for joining a video with its sound track. When missing: a `ToolError`; the file fails and
  is retried on later runs.
- 7-Zip (`7zz` or `7z`), for 7z, RAR and encrypted zip. When missing: a `ToolError`; the archive is
  kept, not extracted.

zip and tar are read by Python. flnd finds each program on your PATH, never downloads one.

First run:

```sh
flnd auth login ins cookies.txt        # log in to Instagram with a cookie file
flnd get ins:example_user         # its own content: posts, reels, stories, ...
flnd status                       # the last run, its failures, logins, the disk
```

Then `flnd sync` gets what is new for every target you have followed.

### The home

The home is `--home DIR`, else `$FLND_HOME`, else `./.flnd`; nothing is written until a command
saves something.

```text
.flnd/
  .lock                 empty; held while a command that writes runs (see below)
  config.toml           settings, optional ([defaults] and [plugins.<code>])
  metadata/flnd.db      files, targets, runs, failures (SQLite)
  media/<code>/<value>/<folder>/...     the downloaded files of each target
  log/<time>_get.jsonl  the run journal; log/crash-<time>.txt a bug's traceback
```

Never share a home with someone you wouldn't give your logins to. `log/` is swept at the start of a
run that downloads: entries older than 30 days go.

| Variable | Used for |
| --- | --- |
| `FLND_HOME` | the home when `--home` is not given |
| `NO_COLOR`, `TERM=dumb` | turn colour off (so does a pipe or a file) |
| `LANG`, `LC_ALL` | a terminal that is not UTF-8 gets `v x * - >` for the glyphs |
| `APPDATA` | on Windows, the user folder (`%APPDATA%\flnd\`) instead of `~/.config/flnd/` |

A setting resolves in this order: a flag for one call, the home's `config.toml`,
the user file (`~/.config/flnd/config.toml`), the default.

## Targets

A target names what to download. Its whole grammar:

```text
target     = text | link
text       = <code>:<value>[|<categories>]      a user, a post, a chat ...
           | <code>?<query>[|<categories>]      a search
link       = http(s)://<host>/<path>[?<query>][|<categories>]   a page on a site
           | <host>/<path>[|<categories>]       when some plugin owns <host>
value      = <name> | <kind>/<id>               a user, one post
categories = all | <name>[,<name>...]
```

`|` is the shell's pipe, so quote a target that has categories: `flnd get
"ins:example_user|tagged"`. A single post is written `<code>:<kind>/<id>` (`ins:post/C1abc`,
`fbk:video/123`); a user name never holds a `/`.

How a target is read, in order:

1. Spaces around it are ignored. Nothing left is `a target can't be empty`.
2. Starting with `http://` or `https://` (any case) it is a link. Otherwise it needs a code and `:`
   or `?` (`[A-Za-z][A-Za-z0-9]*`, any case, spaces allowed before the mark). Without one,
   `<host>/<path>` is a link when a plugin owns the host; anything else is refused.
3. **A link** goes to the first plugin whose domains hold its host (`www.`, `m.`, `mobile.`, `web.`,
   `mbasic.`, `touch.` are dropped, and a subdomain counts). No plugin owns it: `web` takes it. The
   first route that reads the link wins. A link a plugin lists as unsupported is refused with its
   reason, unless a route matches it with more literal path parts. Path parts after the ones a route
   names are ignored (`.../example_user/reels/` is the user), and so are other query keys, so a
   link's own path is never a category list. One no route reads is followed once with a HEAD request
   (a short link of a site with no route for it), then read again. The text after a link's last `|`
   is its categories when every word in it is `all` or a category of the plugin that owns it
   (`instagram.com/example_user|tagged`); else it stays in the link.
4. **`<code>:<value>`** is split at its last `|`: the part after it is the list of categories. A
   route that reads any text (a free title) splits only when every word after the `|` is a
   category or `all`, so `abc:Fans|Lens` stays whole. A one-post route given a list is
   `<code>:<kind>/<id> is one post, with no categories`.
5. **`<code>?<query>`** needs a plugin with a search route (`twt`, `mht`). The text after the last
   `|` is categories only when every word in it is `all` or a category of that plugin; else the
   whole text is the query (`mht?AC/DC`, `twt?a|b`, `twt?url:example.com/x`).
6. **Categories** are split at `,`, trimmed, and empty ones dropped. Names are case-sensitive. `all`
   is every category of the route, opt-in ones too. The result is in the plugin's order, not the
   order typed. With no list, a target the home knows takes the categories it was followed with, and
   a new one takes the route's own (`own`) categories. A search passes its list to every target it
   finds. Naming categories replaces the stored ones.
7. The code is matched in any case; the value is kept as typed. A target's key is `<code>:<value>`,
   or `<code>?<query>`; categories are no part of it. A single post's value starts with its kind, so
   its key is what you type (`ins:post/C1abc`) and never clashes with a user's (`ins:C1abc`). A
   link's `user:pw@` is left out of every key, message and log line.

Refusals, with the text printed (all `UsageError`, exit 2, before any request):

- (empty): `a target can't be empty`
- `example_user`: `which plugin is "example_user"? Write it as tik:example_user, fbk:example_user,
  ins:example_user, ...`
- `nope:x`: `no plugin named nope`; hint `Plugins: tik, fbk, ins, thr, twt, loi, mht, smp, pxv, web`
- `ins:`: `ins: write a value after ins:`; hint `Write it like <every form of the plugin>`
- `ins:%`: `ins: can't read '%'`; hint `Write it like <every form of the plugin>`
- `ins:example_user|nope`: `ins has no category nope`; hint `Categories: posts, reels, tagged,
  reposts, stories, highlights, profile`
- `ins:reel/C1`, `ins:example_user/posts/`, `ins:example_user/tagged`: `ins:reel/C1: reel isn't a
  post kind of ins; write ins:post/<id>, or categories after |: "ins:<user>|tagged"` (every
  single kind of the plugin is listed; a plugin with none gives only the `|` hint)
- Every target's key can be typed back, such as `tik:short/ZMabc123` or `fbk:watch/abc`.
- `tik:post/1234567890123456|all`: `tik:post/1234567890123456 is one post, with no categories`
- `ins?cats`: `ins has no search`; hint `Write it like ...`
- `twt?`: `twt: the query is empty`
- `https://x.com/home`: `twt: that's not a profile`
- `https://www.tiktok.com/@@example_user`: `tik: can't read https://www.tiktok.com/@@example_user`;
  hint `Write it like ...`
- `https://exa mple.com/x`: `no plugin reads links to exa mple.com`; hint `Plugins: ...`
- `example.com/x` (no plugin owns it): `which plugin is "example.com/x"? ...`

A link that a short link led to adds ` (from <what you typed>)` to `can't read`. A mistake in
`--when`, `--limit` or `--folder` is a `UsageError` too ([Dates](#dates), [Folders](#folders)).

### Reading from standard input

`flnd get` reads standard input only when a target is `-`: each line is a target (blank lines and
`#` lines are skipped), put where the `-` was (`cat targets.txt | flnd get -`). `-` may come with
other targets. Standard input is never read otherwise, even when it is not a terminal.

### Searches and `--limit`

- A search (`twt?<words>`, `mht?<words>`, or a search link) finds targets, and each found target
  is got as its own. A search needs `--folder`, which puts all its hits in one place ([A search
  needs a folder](#a-search-needs-a-folder)). A followed search gets only the targets it had not
  found before.
- `--limit N` (at least 1) is at most N posts per target, or N targets per search or other
  discovering route (`mht:author/...`, `mht:genre/...`, `mht:parody/...`, `mht:character/...`,
  `loi:author/...`). `--limit 0` is `limit must be at least 1`.

### Accepted and refused forms by plugin

`<user>`, `<id>`, `<code>` are placeholders. Every `facebook.com` form also reads on `fb.com`, every
`x.com` form on `twitter.com`, `loix.cc` forms on `moegoat.com`. A form with `...` ignores what is
in that place. Messages not shown say `<code>: can't read <text>` with the hint `Write it like
<every form>`.

**ins**

| Form | Route | Reads |
| --- | --- | --- |
| `ins:<user>`, `instagram.com/<user>` (`@` dropped; `A-Za-z0-9._`, 1-30) | `user` | the profile |
| `ins:post/<code>` | `post` | one post |
| `instagram.com/p/<code>`, `/reel/`, `/reels/`, `/tv/`; also under `/<user>/` | `post` | one post |

Refused: a name past that rule is `ins: '<name>' isn't a user name; a user name is 1 to 30
letters, digits, . or _`; `/stories/...` and `/stories/highlights/...` are `ins: story links aren't
supported; ...` with the hint `"ins:example_user|highlights"`; `/explore/` (and `/explore/tags/`),
`/accounts/`, `/direct/` are `ins: that's not a profile`; `instagram.com/` alone.

**thr**

| Form | Route | Reads |
| --- | --- | --- |
| `thr:<user>`, `threads.com/@<user>`, `threads.net/@<user>` | `user` | the profile |
| `thr:post/<code>`, `threads.com/@<user>/post/<code>` (and `threads.net`) | `post` | one post |

Refused: a bad name as `ins`'s; a tab link (`/@<user>/replies`, `/reposts`, `/media`), hint
`"thr:example_user|replies"`; `thr?<words>`: `thr has no search`; `/search`: `thr: there's no
search`; `/activity`, `/settings`, `/login`, other site pages: `thr: that's not a profile`.

**twt**

| Form | Route | Reads |
| --- | --- | --- |
| `twt:<user>`, `x.com/<user>` (any tab link, such as `/media`) | `user` | the profile |
| `twt:<id>`, `x.com/i/user/<id>` (digits) | `user` | the profile with that id |
| `twt:post/<id>`, `x.com/<user>/status/<id>`, `x.com/[i/web/]status/<id>` | `post` | one tweet |
| `twt?<words>`, `x.com/search?q=<words>` (`f=` and the like ignored) | `search` | the Latest tab |

A handle is 1 to 15 letters, digits or `_` (digits of any length: an id); another is refused
(`twt: 'x.com' isn't a handle`), as are `x.com/search` without `q=`, lists, communities and spaces
(`twt: lists aren't supported; ...`), and, as `twt: that's not a profile`, `home`, `explore`,
`settings`, `notifications`, `messages`, `hashtag`, `i` (`i/web/status/<id>` and `i/user/<id>`
read), `intent`, `compose`, `login`; `x.com/` alone.

**tik**

- `tik:<user>`, `tiktok.com/@<user>`: the profile (route ``user``)
- `tik:post/<id>`, `tiktok.com/@<user>/video/<id>`
  and `/photo/<id>`, `tiktok.com/video/<id>` and `/photo/<id>`: one post (route ``post``)
- `vm.tiktok.com/<code>`, `vt.tiktok.com/<code>`, `tiktok.com/t/<code>`: one post, link only (route
  ``short``). It is opened in the browser and read from the video it lands on; a landing that is no
  video or photo post is `NotFound` with the hint `check the link in a browser; only video and photo
  links work`.

`tik:<digits>` is a user name like any other. Refused with `tik: <what> aren't supported; give a
user or a post`: `tiktok.com/tag/...` (hashtag pages), `/music/...` (sound pages),
`/discover/...`, `/live`, `/explore`, `/search...` (search pages). Also refused: `@@user`, any
other `tiktok.com` link.

**fbk**

- `fbk:<user>`, `facebook.com/<user>`, `/@<user>`, `/profile.php?id=<id>`, `/people/.../<id>`: a
  profile or page, one route (``user``); a tab it lacks (`tagged`) logs `no such tab`
- `fbk:video/<id>`, `facebook.com/watch?v=<id>`, `/videos/<id>`, `/<page>/videos/<id>` (or
  `.../<slug>/<id>`), `/reel/<id>`, `/reels/<id>`: one video or reel (``video``)
- `fb.watch/<code>`, `facebook.com/share/p|r|v/<code>`: followed in the browser to the video or post
  they land on (routes ``watch``, ``share``; `fbk:share/r/<code>` types the key back); landing
  elsewhere is `NotFound`, hint `check the link in a browser`
- `fbk:photo/<id>`, `facebook.com/photo.php?fbid=<id>`, `/photo?fbid=<id>`: one photo (``photo``)
- `fbk:post/<owner>_<story id>`, `facebook.com/<owner>/posts/<id or pfbid...>`,
  `/permalink.php?story_fbid=<post>&id=<id>` (or `story.php`): one post (route ``post``)

`m.`, `web.`, `mbasic.` read as `www.`. A profile name is never one of `profile.php people share
watch reel reels videos photo photo.php permalink.php story.php login login.php`. Refused with `fbk:
<what> aren't supported; give a profile or page, or a video, photo or post link`: `/photos`,
`/groups`, `/events`, `/stories`, `/marketplace`, `/hashtag`, `/watch` without `v=`. `photo.php`
without `fbid`, `permalink.php` without both ids: `fbk: can't read <link>`. Any other path after
a profile name is ignored.

**loi**

| Form | Route | Reads |
| --- | --- | --- |
| `loi:<id>`, `loix.cc/lois/<id>`, `moegoat.com/lois/<id>` (digits) | `post` | one post |
| `loi:author/<id>` | `author` | an author's posts |
| `loix.cc/library/loier/<id>`, `moegoat.com/library/loier/<id>` | `author` | the same |

`author` finds `loi:<id>` (`--limit N` is N posts) and needs no `--folder`. Every other page of
both domains is refused: `that page isn't supported; give a post or an author` (`/library...`:
`library pages aren't supported; ...`), and so is `loi:post/<id>` and `loi?<words>`.
`loi:<id>|archive` and `|photo` (and on `loi:author/<id>`) are allowed.

**mht**

- `mht:<id>`, `mimihentai.moe/manga/<id>` (digits): one manga (route ``manga``)
- `mht:chapter/<id>`, `mimihentai.moe/manga/.../chapter/<id>`: one chapter (route ``chapter``),
  filed in its manga's folder as the `chapters` category files it (its own number when the
  answer names no manga)
- `mht:author/<name or id>`, `mimihentai.moe/author/<x>`, `/authors/<x>`: an author's manga (route
  ``author``); `mht:genre/<x>`, `/genre/<x>`, `/genres/<x>`: a genre's (``genre``)
- `mht:parody/<name or id>`, `mimihentai.moe/parody/<x>`, `/parodies/<x>`: a parody's manga (route
  ``parody``); `mht:character/<x>`, `/character/<x>`, `/characters/<x>`: a character's
  (``character``; the name is sent as written, the site has no lookup)
- `mht?<query>`: every manga a search finds (route ``query``)

Query terms: plain words (title), `title:`, `author:`, `genre:` (repeatable), `-genre:`, `parody:`,
`character:`, `chapters:<n>`. Quote values with spaces. Any other term, a `-` before a term but
`genre`, a term with no value, a `chapters:` that is not a number, and an empty query are refused.
`mht:chapter/5|chapters` is `mht:chapter/5 is one post, with no categories`.

**smp**

| Form | Route | Reads |
| --- | --- | --- |
| `smp:<id>`, `simpcity.cr/threads/<slug>.<id>/...`, `.../threads/<id>/` | `thread` | a thread |

`simpcity.su` reads too. Anything after a link's id is ignored (`page-<n>`, `latest`, `#post-<n>`).
Refused:
`simpcity.cr/` alone, `/forums`, `/members`, `/search`, `/tags`, `/whats-new`, `/watched`,
`/conversations`, `/account` and `/posts`, with `smp: that page isn't supported; give a thread or a
post: smp:<id>, smp:post/<id>` (a post route comes in a later step).

**pxv**

| Form | Route | Reads |
| --- | --- | --- |
| `pxv:<id>`, `pixiv.net/users/<id>`, `/<lang>/users/<id>` (digits) | `user` | the profile |
| `pixiv.net/member.php?id=<id>`, `/member_illust.php?id=<id>` (old links) | `user` | the same |
| `pxv:artwork/<id>`, `pixiv.net/artworks/<id>`, `/<lang>/artworks/<id>` | `artwork` | one work |
| `pixiv.net/i/<id>`, `/member_illust.php?illust_id=<id>` (old links) | `artwork` | the same |
| `pxv:novel/<id>`, `pixiv.net/novel/show.php?id=<id>`, `/<lang>/novel/...` | `novel` | one novel |
| `pxv?<words>`, `pixiv.net/tags/<words>`, `/tags/<words>/artworks` | `search` | a tag's artworks |

A link's tab (`/illustrations`, `/manga`, `/bookmarks/artworks`) is ignored. `pixiv.me/<name>` has
no route: the resolver follows it to `/users/<id>`. Only a user's number is a target:
`pxv:example_user` is `pxv: a pixiv user is written by its number: pxv:12345678`, hint `the number
in pixiv.net/users/<number>; a pixiv.me/<name> link works too`. Refused with `pxv: <what> isn't
supported; give a user, an artwork or a novel: pxv:12345678, pxv:artwork/12345678,
pxv:novel/12345678`: `pixiv.net/` alone,
series (`/novel/series/...`, `/user/<id>/series/...`), `/ranking.php`, `/discovery`, `/request`,
`/bookmark_new_illust.php`, `/stacc`, `/settings`, `/dashboard`, a tag page's other tabs,
`sketch.pixiv.net` and `comic.pixiv.net`. `pxv:artwork/5|manga` is `pxv:artwork/5 is one post, with
no categories`. A search needs `--folder`, as twt's does.

**web**

| Form | Route | Reads |
| --- | --- | --- |
| any `http(s)://` link no other plugin owns | `page` | a file, a stream, or a page's media |
| `web:http(s)://...` | `page` | the same, even for a host another plugin owns |

Refused: `web:example.com` is `web: can't read 'example.com'` (hint `Write it like web:<url>`); a
trailing `|all` is `web:<link> is one post, with no categories`.

## Dates

`--when WHEN` keeps the posts of some days. `--when` also bounds a sync. Left out, it is `[defaults]
when` in `config.toml`, else every day. `--when ""` is every day, whatever the config says.

| WHEN | Keeps | Since | Until |
| --- | --- | --- | --- |
| `2026` | that year | 2026-01-01 | 2026-12-31 |
| `2026-06`, `2026-6` | that month | 2026-06-01 | 2026-06-30 |
| `2026-06-15`, `2026-6-5` | that day | the day | the day |
| `2026-01..2026-03` | first day of the first to last day of the second | 2026-01-01 | 2026-03-31 |
| `2026-01..` | from then on | 2026-01-01 | none |
| `..2026-03` | up to then | none | 2026-03-31 |
| `..`, `""` or spaces | every day | none | none |
| `30d`, `8w`, `1y` | back from today (`w` is 7 days, `y` 365) | today minus the span | none |

Both ends of a range may be any of year, month or day (`2026-01-05..2026-02`). Both ends are
included. `0d` is today and after. Anything else is refused, `today`, `30D`, `1m`, `2026-13`,
`2026-02-30`, `20260615`, `99`, and a range that ends before it starts (`2026..2025`):

```text
when takes a year (2026), a month (2026-06), a day (2026-06-15), a range of those
(2026-01..2026-03, 2026-01.., ..2026-03), or a span back from today (30d, 8w, 1y)
```

That is one line, a `UsageError` with exit 2. In `config.toml` a bad `[defaults] when` says `<file>:
defaults.when is text such as "1y", "2026" or "2026-01..2026-03", or "", not this text`.

How a date is compared:

- A post's day is the date the plugin gives for it, as a **UTC** day. A date the plugin gives that
  is not from 2004 to next year counts as none.
- `30d` counts back from **today's date on this computer's clock** (local time).
- A post with no date is **kept**: `--when` never drops it. The engine dates it from its nearest
  dated neighbour in the listing, or from an earlier run.
- A listing that comes newest first stops after 5 posts in a row older than `since`; pinned posts do
  not count. `until` never stops a listing: newer posts are skipped one by one.
- A target whose posts carry no date at all (`web`) is read whole: `--when` keeps everything.
- `twt` searches also send `--when` to X as `since:` and `until:`.

## Categories

A category is one part of a target that can be got alone. Each plugin's list is in
[Plugins](#plugins); `own` ones are the account's own content and are taken by default, opt-in ones
(other people's media) only when named or with `all`.

| Plugin | Own (default) | Opt-in |
| --- | --- | --- |
| `ins` | `posts reels stories highlights profile` | `tagged reposts` |
| `thr` | `posts replies profile` | `reposts` |
| `twt` | `posts media highlights replies profile` | `reposts` |
| `tik` | `videos stories playlists profile` | `reposts liked favorites` |
| `fbk` | `posts highlights photos albums reels videos stories profile` | `tagged` |
| `loi` | `archive photo` | none |
| `mht` | `chapters cover` | none |
| `smp` | `images videos` | none |
| `pxv` | `illusts manga ugoira novels profile` | `shiori novelshiori` |
| `web` | none | none |

Write the list after `|`, and quote the target, since `|` is the shell's pipe:

```sh
flnd get ins:example_user                    # the own categories
flnd get "ins:example_user|posts,reels"      # these two
flnd get "ins:example_user|tagged"           # only the opt-in one
flnd get "ins:example_user|all"              # everything, opt-in too
flnd get "instagram.com/example_user|tagged" # a link takes a list the same way
```

- `flnd get "<target>|<list>"` follows the target with that list: later syncs take the same.
- A single post has no categories (naming one is `<code>:<kind>/<id> is one post, with no
  categories`); `loi:<id>` is the exception.
- `"mht:author/<x>|<list>"` (and the genre, parody and character ones), `"mht?<q>|<list>"` and
  `"twt?<q>|<list>"` pass the list on to each target found. With no list, each takes its own.
- A category that is not newest first (`tagged`, `stories`, `profile` ...) is read to its end on
  every run; only newest-first ones stop early ([Sync](#sync)).

## Folders

Each target has a folder, `media/<code>/<value>/`, where `<value>` is the target's value made safe
as a file name. (A single post is the exception, below.) Under it a file goes where its plugin's
`folder` template puts it, named by its `filename` template, with the extension its content calls
for:

```text
media/ins/example_user/posts/2026-01-02_C1abc_0.jpg
media/ins/example_user/highlights/Travel/2026-01-02_1234567890.mp4
```

A template is picked for each file: the category's own, else the route's, else the plugin's. A
file's modified time is its post's day at noon UTC. A file listed in two categories of one target is
saved once, and the second gets a hard link (else symlink; [For developers](#for-developers)).

### A single post

A single post (a link, or `<plugin>:<kind>/<id>`) goes in its owner's folder, by the category the
site's data gives it, with that category's own `folder` template, so `{category}` and a `--folder`
list entry for the category apply under `media/<code>/<owner>/` (`<owner>` made safe as a file
name):

| post | category | folder |
| --- | --- | --- |
| `ins` post | `posts` | `media/ins/<username>/posts/` |
| `ins` reel (a clip) | `reels` | `media/ins/<username>/reels/` |
| `thr` post; a reply | `posts`; `replies` | `media/thr/<username>/posts/`; `.../replies/` |
| `twt` tweet; a reply | `posts`; `replies` | `media/twt/<handle>/posts/`; `.../replies/` |
| `tik` video; photo post | `videos` | `media/tik/<user>/videos/`; `.../images/` |
| `fbk` video, reel, photo | `videos`, `reels`, `photos` | `media/fbk/<owner>/<category>/` |
| `fbk` post | `posts` | `media/fbk/<owner>/posts/` |
| `loi` post | `archive`, `photo` | `media/loi/<author>/<post> <title>/` |
| `pxv` artwork, novel | `illusts` `manga` `ugoira`; `novels` | `media/pxv/<name>_<id>/<cat>/` |
| `pxv` bookmark | `shiori`; `novelshiori` | `media/pxv/<name>_<id>/<cat>/` |

The owner is the post's: `user.username`, `screen_name`, `uniqueId`, a Facebook vanity name else its
id. With no owner in the data the post goes where it always did, `media/<code>/<post id>/`, with no
category folder, and a log line says so. Any route declared `single` is filed this way, whatever
its name (`artwork`, `novel`); `web` and `mht`'s chapter never give an owner, so they sit under
their id. `loi` declares `root = "{author}"`, so its posts sit in `media/loi/<author>/`. A search's
hits ignore all this: they go to the search's `--folder` ([A search needs a
folder](#a-search-needs-a-folder)). Files saved earlier stay where they are, and keys are
unchanged, so a post saved by link is skipped, and found in place, when its profile is synced
later.

### `root`

A plugin may name its targets' folders with `root`, a class attribute that is a template like
`folder`, `"{target}"` by default: the folder of a target under `media/<code>/`. It is filled for
each file with the same fields, and follows the same rules as `folder` (the cleaner, an empty
field dropping its separator, containment). The file goes to
`media/<code>/<root filled>/<folder filled>/<filename>`. `{target}` of a single post is its owner,
else its id. `root = "{target}_{name}"` files `example_user`'s posts, with the post field `name`
`Example`, in `media/<code>/example_user_Example/`; an empty `name` gives `example_user`. `root` is
no `config.toml` key. `--folder` replaces `root` and `folder` both, for the categories it names; a
category it leaves out keeps its default place, under `root`. A single post's `--folder` templates
are under its owner's folder, as above. With a root of post fields, a run sweeps no stale parts of
a target's folder, as with a `--folder` of fields unless the target already keeps its root.

**A root is kept.** The first time a target with a non-default `root` has a file queued, the engine
stores the root as rendered for that file, in `state` under the plugin name `""` (no plugin has
it, so no `ctx.state` key can clash) and the key `root:<code>:<target value>`. Every later file of
the target uses the stored text and renders `root` no more, so an account renamed on the site
never splits across two folders. A single post is kept under its owner's key
(`root:<code>:<owner>`), so a work and its owner's profile share one folder, whichever came
first; a post with no owner is kept under its own target. `--dry` and the sweep of stale
parts read the stored root too. A `--folder` run neither reads nor writes it, and a plugin whose
`root` is the default `{target}` stores nothing. Files saved before a root was stored never move.

### `--folder`

`--folder VALUE` replaces the folder templates, the plugin's, route's,
category's and `config.toml`'s, for the categories it names, for the run. Files go in
`media/<code>/<rendered>/<filename>`: the target's own folder is not added. Every target a search
finds takes it too. `VALUE` is one template for every category, or a list that gives each category
its own ([One folder per category](#one-folder-per-category)).

```sh
flnd get twt:example_handle --folder=example_handle
flnd get twt:example_handle --folder="example_handle/{date}"
flnd get "twt?@example_handle -from:example_handle" --folder="{query}"
flnd get ins:example_user --folder="{target}/{date:%Y-%m}"
```

Grammar:

```text
template = segment[/segment...]             not empty
segment  = text and {field} / {field:format} / {field.attr} / {field[0]}; {{ and }} are braces
```

| Field | Value |
| --- | --- |
| `plugin` | the plugin's code (`ins`) |
| `target` | the target's value (`example_user`; a search's found target) |
| `category` | the post's category (`posts`; empty for a single post) |
| `post` | the post's id |
| `date` | the post's day, `2026-01-02`; empty with none; takes a format, `{date:%Y-%m}` |
| `num`, `count`, `index` | file number from 0, the post's file count, `_<num>` or empty |
| `extension` | the file's extension, with its dot |
| `dir`, `file` | `path`'s folder and its name |
| `query` | a search's words; empty for any other target |
| `path` | accepted, but only filled in `key` templates; empty in a folder |
| plugin fields | each plugin's `fields`, listed below the table |

A text field takes a length, as Python's own format rule does: `{title:.40}` is at most 40
characters of `title`, and the cleaner then runs on the cut text. `flnd.plugin.headline(text)`
makes a title of a caption: its first non-empty line, with `#tags`, `@names` and `http(s)` URLs
removed, whitespace squeezed to single spaces and trimmed ("" for nothing left).

Plugin fields: `ins` `album title`; `thr` `title`; `twt` `part n title name`; `tik` `part album idx
title`; `fbk` `part album`; `loi` `author title part`; `mht` `title order author genres chapters
name`; `smp` none; `pxv` `title user name`; `web` `name`; a plugin of your own declares its
`fields`.

Rendering: each `/` segment is filled alone. A missing or empty field drops the separators
(`_ - space . : ,`) next to it, and a segment that comes out empty is left out, so `{date}_{target}`
without a date is `<target>`. Each rendered segment goes through a filename
cleaner (`/ \ : * ? " < > |` and control characters become `_`; runs of spaces and
underscores become one `_`; leading and trailing dots, spaces and underscores go; 200 bytes at
most), and the final path must stay in `media/<code>/`.

Refused before any request (`UsageError`, exit 2; the message names where it came from, `the call`
for `--folder`; the hint is `fix folder in the call, or remove it for the default`):

- `/a`, `a/` (leading or trailing `/`): `the call: folder is a template inside the target's folder,
  not this text`
- `a//b`, `a/ /b` (an empty segment): the same
- `a\b` (a backslash), `../a`, `a/./b` (`.` or `..`), `C:/x` (a drive), a control character: the
  same
- `a/{x`, `a/{}` (bad braces): `the call: folder is a template whose braces each hold one field
  name, not this text`
- `{part}` on `ins` (a field its plugin lacks): `the call: folder is a template of the fields album,
  category, count, date, dir, extension, file, index, num, path, plugin, post, query, target,
  not this text`

The field check runs once the targets are known, for each target's plugin. A template that passes
the checks but would leave `media/<code>/` stops the run with `folder template escapes the plugin
media folder`. The same rules check `[plugins.<code>] folder` in `config.toml`, where the message
starts with the file and key (`.flnd/config.toml: plugins.ins.folder is ...`).

### One folder per category

A value is one template (as above), or a comma list of `<category>:<template>`, where `*` is every
category the list doesn't name:

```sh
flnd get "ins:example_user|all" --folder="posts:{target}/feed,*:{target}/other"
```

```text
value = template  |  entry[,entry...]       a list when the first entry starts with <category>:
entry = <category>:<template>  |  *:<template>
```

- Commas inside `{}` don't split (`posts:{date:%Y,%m}` is one entry). A template with a comma
  outside `{}` is a list entry, so a one-template value can't hold one after `<name>:`.
- A category named in the list uses its template; any other uses `*:`; a category with neither keeps
  its default place, `media/<code>/<value>/<the plugin's folder>`, so a list can move one category
  and leave the rest. Its history keys are what they were.
- Every template is rooted at `media/<code>/`, as one template is, and follows the rules and fields
  above. `flnd get --dry` prints each category's rendered folder for its first post
  (`ins:example_user: reels goes to media/ins/example_user/reels`).
- `[plugins.<code>] folder` in `config.toml` takes the same string, and its templates are under the
  target's folder, as a single one always was: `folder = "posts:{part}/a"` moves `posts` and leaves
  the others. A named category there wins over the category's own `folder`; `*` or one template is a
  default the category's and route's `folder` still win over. A category the call's `--folder` names
  or covers with `*` ignores `config.toml`.

Refused before any request (`UsageError`, exit 2), besides the template checks above, which name the
entry as `folder[<category>]` (`the call: folder[posts] is a template of the fields ...`;
`folder[*]` for `*`; `folder` when the value is one template):

- `posts:a,b`: `folder 'b' isn't <category>:<template>`; hint: `write one template
  ("{target}/{category}"), or a list of <category>:<template> entries ("posts:{target}/feed,
  *:{target}/other"); * is every other category`. Every refusal below with "the same hint" says
  this.
- `posts:a,{x}/b`: `folder '{x}/b' isn't <category>:<template>`; the same hint
- `posts:`: `folder posts: the template is empty`; the same hint
- `""`: `folder: the template is empty`; the same hint
- `posts:a,posts:b`: `folder posts: named twice`; the same hint
- `*:a,*:b`: `folder *: named twice`; the same hint
- `nope:a`: `folder nope: no plugin of these targets has this category`; hint `Categories: posts,
  reels` (the categories of the targets' plugins)
- `posts:{x}` (a field `posts`' plugin doesn't declare): `the call: folder[posts] is a template of
  the fields <fields>, not this text`; hint `fix folder[posts] in the call, or remove it for the
  default`

A category name has two characters or more, so `C:drive` is still read as a path, and refused as
one. The same checks run on `[plugins.<code>] folder`, with the file and key in front
(`.flnd/config.toml: plugins.ins.folder is a list of the categories posts, reels, ..., not this
text` for a category the plugin lacks).

### A search needs a folder

A search (`<plugin>?<query>`, or a search link) is refused before any request unless `--folder`
is given and places every category: one template, or a list with `*`.
`config.toml`'s `folder` doesn't count. A search's hits all go there, not to their owners' folders.

- `flnd get "twt?cats"`: `twt?cats: a search needs --folder`; hint `add --folder "{query}"`
- `--folder "posts:{query}/posts"`: `twt?cats: a search needs --folder to place every category`;
  hint `end the list with *:<template>, as in --folder "posts:{query}/posts,*:{query}/other"`

```sh
flnd get "twt?cats" --folder="{query}"
flnd get "twt?cats" --folder="posts:{query}/posts,*:{query}/other"
```

A followed search keeps its folder in its row, and `flnd sync` reuses it, as does
naming the search again without a `--folder`. Giving it another `--folder` is refused unless
`--again` is also given, which keeps the new one (files already saved stay where they are):

```text
twt?cats: this search is saved under --folder '{query}', not 'other'
hint: add --again to use the new folder; files already saved stay where they are
```

A followed search always has its folder; a sync of one without it is refused like an unsaved
search (`a search needs --folder`). `mht:author/...` is not a search, and needs no folder.

### Names and keys in `config.toml`

```toml
[plugins.twt]
folder = "{part}"                  # the plugin's own default, here
filename = "{post}_{n}"            # drops the leading date
```

`filename` (no extension: the engine adds it) and `folder` replace the plugin's templates, and take
the same fields and checks. A category's own `folder` still wins over the plugin's. The history key
(`key`) and `root` are the plugin's and cannot be set.

### Parts

- Every file is written as `<name>.part` and renamed once whole, so a real name is a whole file. A
  stopped download resumes from its part (`Range`, with an `If-Range` validator). Parts nothing
  touched for 7 days are swept when a run starts on the target.
- Archives (`.zip .7z .rar .tar*`) are extracted beside themselves into a folder of their name, and
  the archive stays. `File(unpack="here")` instead extracts into the post's folder, lifts a lone top
  folder's contents up one level, saves a clash with a file there as `name (2).ext` (nothing is
  replaced), and deletes the archive once extracted (kept on failure); the row keeps the file list.
- `File.frames(url, delays, *, keep=False)` is a zip of numbered frames that is saved as one MP4.
  `delays` is a non-empty list of `(frame file name, milliseconds)` pairs in play order, each
  delay a positive whole number; a plugin that gives anything else, or a URL that isn't http or
  https, fails with `PluginError`. The zip is downloaded as a part, like any URL file, then one
  merge job unzips it into a scratch folder (deleted after), checks that every entry is named
  like `000000.jpg` (six digits, then `jpg`, `jpeg`, `png` or `gif`, no folder) and that the
  entries are exactly the names in `delays`, and runs ffmpeg on an ffconcat list (each delay in
  seconds, the last frame listed once more so its delay counts). The video is H.264 in an MP4
  (`-crf 18`, `yuv420p`, sides padded to even, `+faststart`), and its extension is `.mp4`. A zip
  that doesn't match fails the file as `Changed`; a missing ffmpeg is a `ToolError`, as for DASH.
  With `keep=True` the zip also stays beside the video as `<name>.zip`, never extracted; the
  video's row lists it, so `status` and `check` don't take it for a stray file.

## Sync

`flnd get` follows each target that is not a single post, so `flnd sync`
keeps them current. `flnd sync <pattern>...` syncs only the followed targets whose name matches any
glob pattern (`*`, `?`, `[seq]`, case-insensitive), such as `flnd sync "ins:*"`. Both `get` and
`sync` always check followed targets for new content even after they are complete (so
non-newest-first categories like stories still check on every run), while newest-first feeds stop
early. What a run skips and where it stops:

- A saved file: skipped: its row says saved and the file is there, or the file is on disk at its
  planned path
- A gone file: a 404 or 410 records it `gone`; it is not asked for again unless `--again`
- Failed files: each `get` of a target first retries that target's failed files, on at most 3 later
  runs per file; a post is listed again from its page's cursor for fresh links
- Caught up, one rule: a newest-first category keeps its head, `{"id", "ts"}`: its newest post by
  time (the first of a tie; a post with no time is the head only when none has one), with that time
  (Unix seconds when the site gives a time of day, else its day). A post is old when it is the head,
  or strictly older than the head's time (never equal: posts of the head's second or day may be new;
  a day against a time compares days; a post with no time is never old by time). A sync stops after
  4 old posts in a row; a new post resets the count, and a post marked `pinned` is skipped. No pin
  mark is trusted: 4 is above every site's pin limit (ins 3, tik 3, twt 1, thr 1). A stored head
  that is not an `{"id", "ts"}` object counts as no head. A plugin may read that head as
  `ctx.head`, a read-only mapping, to ask its site for less; it is None on a first read, with
  `--again`, for a category that isn't newest first, on a single route, and outside a category's
  listing. The stop rule above still decides where the listing ends. A listing with nothing new is
  one request when the site pages by cursor (`Skipped`, reason `nothing new`). A
  search keeps the key of its first target as its head, and a sync stops at it (`--when` goes on to
  the search)
- Older than `--when`: after 5 posts in a row older than its start
- A cut-short run: a full download keeps its cursor, and its head, and resumes there; once it ran
  out it reads the newest down to that head. A sync that is cut, or fails, leaves its head: the next
  one reads down to it again, and the saved posts cost no request. The head moves only when a pass
  reaches the head or the end of the listing
- Not newest first: read to the end on every run

`--again` lists every post again, past the head and saved cursors, and asks again for gone files.
Files saved on disk are still skipped, so it downloads only what is missing. Use it when a creator
unarchives an old post or a missing file comes back.

`--dry` saves and writes nothing, and takes no home lock, so it can run beside a real run. It
prints `found <target>` for each target a search finds, then `new <path>` or `saved <path>` for
each file, and a count.

Ctrl-C: the first stops listing and lets files in progress finish; the second, or a SIGTERM, stops
at once and leaves `.part` files to resume (what still runs 2 s later is cancelled, and 5 s later
the process quits); the third quits at once, exit 130.

One run per home. `get`, `sync`, `target clean` and `status --fix` hold an operating-system lock
on `<home>/.lock` from before they write anything until they end, so a crash never leaves a stale
lock. A second one stops at once with `another flnd run is using <home>`, hint `wait for it to
finish, or use another --home`, exit 2. `target follow`, `unfollow` and `retry`, `status` without
`--fix`, `target list`, `auth` and `config` never take it, so you can follow a target while a sync
runs: that write waits up to 30 s for `flnd.db`, while the run's own commit never waits, and goes
back in the queue until the write is done.

## Logins

One login per plugin, kept as a file in the logins folder `~/.config/flnd/logins/`
(`%APPDATA%\flnd\logins\`), made with mode 0700, beside the user's `config.toml`. It is shared
by every home and never inside one, so a copied home carries no login.

`flnd auth login <plugin> [file]` logs in. For a cookie plugin it takes the path of a Netscape
`cookies.txt` or a JSON cookie export, left out on a terminal it asks for it. A plugin may declare
its own way instead, named in `Login.methods`; the cookie file is the built-in plugins' way.

- a cookie login keeps only the cookies of the plugin's domains, as Netscape text, in
  `logins/<code>.txt` (mode 0600, written through a temp file and an atomic replace), and replaces
  any earlier login. It is refused unless one of the plugin's login cookies is there, and unless
  one of them is still good (a session cookie always is). A cookie plugin's login cookies are in
  [Plugins](#plugins). A plugin declares them as `cookies("sessionid", "ds_user_id")`, and
  `cookies(match=r".+_user")` also accepts any cookie whose whole name matches the expression.
- a plugin's own login keeps what its `Login.start` wrote to `ctx.secrets` in
  `logins/<code>.json` (mode 0600), which `ctx.secrets` reads. A cookie's value or a secret is
  never printed, logged or put in an error: messages name the file, the line or the cookie.

Refusals:

- unknown plugin: `unknown plugin '<code>'`, hint `flnd --help lists the plugins`
- a way the plugin lacks: `<code> logs in by <methods>, not '<method>'`
- a plugin with no login (`mht`, `web`): `<code> needs no login`
- no readable file: `<code>: a file login needs the path of a cookie file`, hint `flnd auth login
  <code> <file>`; on a pipe, `<code>: which cookie file?`
- no login cookie: `<code>: no login cookie (<names>) in <file>` (`a name matching <pattern>` for a
  `match`), hint `export the cookies from a browser where you are logged in`
- every login cookie expired: `<code>: the login in <file> has expired`, hint `export a fresh
  cookie file`

Using it:

- `flnd auth list`: one row per plugin with a login: plugin, status (`ok`, `expires in 2 days`,
  `expired`) and its file. A plain table.
- `flnd auth logout <plugin>`: deletes its files. A terminal is asked first.
- **A run** reads `logins/<code>.txt`, and warns when a login cookie has expired or expires within
  3 days. When the site refreshes a cookie the file already holds, the run writes it back to the
  file atomically (mode 0600), so a rotating session stays alive.
- **Refused.** A 401 or 403 on the plugin's domains, or a login wall the plugin declares (a page
  that lands on it waits 30 s, then 2 min, then counts as refused), fails that plugin's targets
  with the hint `export a fresh cookie file, then flnd auth login <code> <file>`. Nothing is
  marked: the next run tries again.
- **Holds.** A block holds the plugin, not a login: 30 s, 2 min, 8 min, then the target fails.

## When it goes wrong

Exit codes:

- 0: all worked
- 1: some files or targets failed (named above the exit); `flnd status` exits 1 when something needs
  care
- 2: a usage error: unknown command or flag, a target no plugin reads, a category its route lacks, a
  bad `--when`, `--limit` or `--folder`, or another run using the home
- 3: a login is needed, was refused, or `flnd auth login` failed
- 4: flnd itself stopped: a full or refused disk, a missing tool, a broken plugin, a bug
- 130: Ctrl-C

A mistake you can fix prints `✗ <message>` then `  hint  <what to do>`, never a traceback. A flag
mistake is `✗ flnd: unrecognized arguments: --nope` with the hint `flnd -h lists what it takes`. A
bug prints `✗ flnd hit a bug: <error>` and `hint  details in <home>/log/crash-<time>.txt` (exit 4;
`-v` prints the traceback instead).

Every error is one of the classes below, with `str(e)` (one line, no secret), `hint`, `target`
and `retryable`.
Exit codes go by class, never by name.

```text
Error
├── UsageError     exit 2   a bad target, flag or setting; raised before any request
├── LoginError     exit 3   not logged in, expired, refused (401 or 403 on its domains)
├── SiteError               the site answered, but not usefully (.status, .response)
│   ├── Blocked             rate limited or blocked (.wait seconds, or None)
│   ├── NotFound            404 or 410: recorded gone
│   ├── Hidden              it exists, but this login cannot see it
│   └── Changed             the answer no longer has the expected shape
├── NetworkError            DNS, TLS, timeout, reset, a cut transfer
├── DiskError      exit 4   no space, no permission, a long name (.path); stops the run
├── ToolError      exit 4   ffmpeg, 7zz or a browser is missing (stops the run) or failed
└── PluginError    exit 4   a broken plugin class, or a bug escaping a listing
Stopped(KeyboardInterrupt)  exit 130, never an Error
```

Any other `Error` exits 1. What the engine does, by cause:

- timeout, reset, cut connection, 5xx (`NetworkError` / `SiteError`): retried up to 4 times after
  about 1, 2, 4, 8 s (each x0.5 to 1.5); then the file fails alone
- bad protocol or URL, no DNS, too many redirects, bad certificate (`NetworkError`): not retried;
  the file fails
- 429 (`Blocked`): the host's bucket waits `Retry-After` or the reset (at most 1 h), else 30 s, and
  its pace halves for 20 requests; a third 429 in a row is a block round
- a block a plugin reads from a page (`Blocked`): the plugin is held 30 s, then 2 min, then 8 min;
  ten quiet minutes restart the rounds; the fourth round fails the target
- 404, 410 (`NotFound`): never retried; the file is recorded gone, the target goes on
- not visible to this login (`Hidden`): the job fails
- 401, 403 on the plugin's domains (`LoginError`): not retried; the plugin's targets fail with a
  hint to log in again
- a login wall (`LoginError`): waits 30 s, then 2 min, then as a 401
- expired link (403, 410 on a lazy post's file): the post's files are listed again once
- unreadable answer (`Changed`): never retried; a scrubbed sample is kept in `log/<run>/samples/`
- `got a web page instead of a photo` (`SiteError`): the file fails; usually a login wall
- disk full, refused, name too long (`DiskError`): the run stops; saved files keep their rows
- `flnd.db` locked by another process: queued again every 20 ms up to 30 s, then the run stops (`let
  the other run finish`)
- `ToolError`: a missing ffmpeg, 7zz or browser stops the run; a failed DASH join is retried
- an exception in a listing (`PluginError`): the target fails with the bug hint

Failing means: a file is counted failed, written to `failures`, and retried at the start of its
target's next runs (at most 3 later runs; after that run `flnd target retry <target>`);
a target whose listing fails prints its error and hint and the other targets run; a usage mistake
stops the command before anything is fetched.

Messages you will meet:

- `<code>: not logged in, using public access; run flnd auth login <code>`: warning: only public
  posts
- `<code>: the site refused the login (HTTP 403)`: the login expired or was revoked: log in again
- `<home>/flnd.db: locked by another process, for 30s`: wait, or use another `--home`
- `unexpected <Type>: ...`: a bug: run with `-v` and report it

`classify` gives an error without a hint flnd's: a login error about a target gets `export a fresh
cookie file, then flnd auth login <code> <file>`; a full or quota disk `free some space on that
disk, or use another --home`; a read-only one `that disk is read-only; use another --home`; a long
name `use a shorter --home or folder template`; any other exception from a plugin becomes a
`PluginError`, `this is a bug; run with -v and report it`. A URL in a message loses its user
info, query and fragment; secret headers become `***`; samples replace values under keys like
`token`, `cookie`, `session`, `auth`, `password`, `secret`, `key`, `sig` with `...` and never keep a
login's answer.

### check and repair

`flnd status` shows the last run that downloaded (its command, times, saves, failures grouped by
target: 10 shown, then "and N more"), the logins that are expired or expire within 3 days,
and the disk against the history. `check` compares the rows with the disk:

- **missing**: a row's file is gone. A repair removes the row; `flnd get <target> --again` then
  fetches it.
- **wrong size**: the repair takes size and time from the disk.
- **unknown**: a file under `media/` with no row. Reported, never changed, so the exit stays 1.

Never unknown: parts, hidden files, symlinks, hard links of known files, extracted archive
folders, and files renamed into place in the last minute. On a terminal `flnd status` asks `Repair 2
rows? (y/N)`; `flnd status --fix` repairs without asking. It never deletes, moves or downloads a
file. Failure kinds: `network blocked site changed login disk tool bug usage gone error`.

A home from an older schema is refused with `<home> is from an old flnd; start a new home`.

## Commands and flags

`flnd --help` lists every command and the plugins' codes, importing no plugin; `flnd get --help`
adds a row per plugin with its targets and categories. Global flags go before or after the
command.

| Global flag | Default | What it does |
| --- | --- | --- |
| `--home DIR` | `$FLND_HOME`, else `./.flnd` | the home |
| `-v`, `--verbose` | off | show debug lines and a bug's traceback |
| `-h`, `--help` | | print help for flnd or the command |
| `-V`, `--version` | | print `flnd <version>` |

**`flnd get [target...]`**: get what the targets have, and follow them. It needs a
target: with none it is refused (`flnd get needs a target; flnd sync syncs the followed ones`,
exit 2). A target `-` reads targets from standard input. `flnd sync` gets the followed ones.

| Flag | Default | What it does |
| --- | --- | --- |
| `target...` | required; `-` is stdin | see [Targets](#targets) |
| `--when WHEN` | `[defaults] when`, else every day | see [Dates](#dates) |
| `--limit N` | none | at most N posts per target, or N targets per search; N at least 1 |
| `--dry` | off | list what would be saved; write nothing |
| `--folder VALUE` | each target's folder | a template or a list ([Folders](#folders)) |
| `--again` | off | list every post again; ask again for gone files |

**`flnd sync [pattern...]`**: sync followed targets whose name matches any glob pattern (`*`, `?`,
`[seq]`, case-insensitive), or every followed target when none is given. Prints `nothing to sync`
and exits 0 when none matches. Takes `--when`, `--limit`, `--dry` and `--again`.

**`flnd target list`** the targets the home knows, as a table: followed, categories, status,
files, size, last run.

**`flnd target follow <target...>`** follows known or new targets, so `get` and `sync` take them;
nothing is downloaded. **`flnd target unfollow <target...>`** stops following known targets; their
files and history stay. **`flnd target retry <target...>`** makes the failed files of known targets
retryable, so their next `get` tries them again (a file the site said is gone is never retried).
A target that is wrong, or that a command needs known and the home doesn't know, is a usage error.

**`flnd target clean <target...> [--media]`** forgets the targets (rows, files rows, failures,
cursors, heads). An unknown target is a usage error. `--media` (default off) also deletes
their recorded files, links, extracted folders and the folders left empty; files no row
names are never deleted. A terminal is asked first.

**`flnd auth list | login <plugin> [file] | logout <plugin>`**: see [Logins](#logins).

**`flnd status [--fix]`**: see [check and repair](#check-and-repair). `--fix` repairs the history
rows of missing or resized files without asking.

**`flnd config`** prints each key, value, default and origin (`home`, `user`, `default`), then the
two files to edit by hand: this home's `config.toml`, and the user file for every home.

What a run prints: on a terminal a live screen on standard error (a row per target, files in
flight, the `now` line); on a pipe only the end summary. The end summary goes to standard output
(`done in 4m 12s · 3 targets · 1,021 saved (3.3 GB) · 1,220 already saved · 1 failed`, starting
with `✓` when nothing failed); up to 10 failures follow on standard error, each with its message
and hint, then `details  flnd status`. Warnings repeated within 10 s fold into one line,
`text (x2)`. Text from a site is stripped of control codes before it is printed. Each run's events
go to `log/<time>_get.jsonl`.

## Config

`config.toml` in the home, and the user file (`~/.config/flnd/config.toml`,
`%APPDATA%\flnd\config.toml`), hold optional settings. A bad value stops the command with a usage
error naming the file, the key and what it takes, never the value: `config.toml: plugins.ins.pace is
a number of seconds, 0 or more, not this text`. An unknown key or table is a warning, said once, and
ignored. `flnd config` shows every key.

`[defaults]`:

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `when` | text | `""` | days a get takes, as `--when` writes them; also bounds a sync |
| `slowest` | text | `"0"` | cut a transfer under this speed over 30 s and go on |
| `timeout` | text | `"10m"` | abort a stalled download after this (`"10m"`, `"600s"`, `"0"` off) |

`[plugins.<code>]` (one table per plugin):

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `pace` | number | the plugin's | seconds between page requests of one bucket; 0 or more |
| `limits` | table | the plugin's | `"<host>" = n` files at once (n at least 1), merged over its |
| `proxy` | text | none | `http://`, `https://`, `socks5://` or `socks5h://` with host and port |
| `folder` | text | the plugin's | folder template ([Folders](#folders)) |
| `filename` | text | the plugin's | file name template |

A plugin's own settings (its `Settings` struct) are keys of its table and reach it as
`ctx.settings`. Notes:

- Before a file of known size starts its disk needs room for it, the bytes due from files in
  flight, and 64 MiB; else `DiskError`.
- `proxy` carries every request and download of the plugin's HTTP session, not the browser, so
  plugins that open pages (ins, tik, fbk, twt) load them directly and warn once. `socks5h://` looks
  names up through the proxy; the URL may hold a user and password.
- `pace` and `limits` win over the plugin's class. A host's cap still adapts under the file's
  number.

```toml
[defaults]
when = "1y"

[plugins.ins]
pace = 3.0
limits = { "cdninstagram.com" = 8 }
proxy = "socks5://127.0.0.1:1080"
```

## Plugins

One section per built-in plugin, in the order links are tried (`web` last). Targets and
refusals are in [Targets](#targets). Each section lists categories (`own` ones are the default,
`opt-in` ones need a name or `all`; NF marks newest-first, the only ones that stop early), folder,
file name and history key templates, login, limits and walls, and known gaps. Everything here is the
code's own declaration; `root` is under `media/<code>/` (default `{target}`, the target's
value), `folder` under the root.

Shared by every cookie login: method `file`, a cookie file's path; the cookies of the
plugin's domains are kept; a run warns when the login cookie expires within 3 days. Walls and blocks
are held 30 s, 2 min, 8 min by the engine; the fourth block in a row fails the target. Pace is the
least seconds between two page requests of one bucket; each CDN host's files go 8 at once, adapting
up to 32.

### ins (Instagram)

- **Reads:** a profile's posts, reels, tagged, reposts, stories, highlights and profile picture; one
  post or reel by link. Logged in, a post uses the web API; profile tabs scroll until a paginated
  GraphQL POST (`variables.after`) is captured, then continue in-page (or keep scrolling). A 429 on
  a post's info holds and asks again; only an unreadable answer opens that post's page.
- **Categories:** `posts` NF, `reels` NF, `stories`, `highlights`, `profile` (own); `tagged`,
  `reposts` (opt-in). A pinned post does not count toward the stop for older posts. A repost
  carries the original post's date, so `reposts` is read to its end.
- **Folder:** `{category}`; `highlights/{album}` (a folder per highlight); `profile`.
- **Filename:** `{date}_{title:.40}_{post}{index}` (`title` is the caption's first words, see
  `headline`; empty, it leaves no `_`); stories, highlights and the picture keep their names; a
  story with music also saves `..._music.m4a`.
- **Key:** `{post}_{num}` (`ins:C1abc_0`); `stories`, `highlights`, `profile` key by `{path}`.
- **Login:** cookie `sessionid` (optional).
- **Limits and walls:** pace 2.0; no host limits; wall `/accounts/login`. Lock/challenge walls
  (`/challenge`, `/accounts/suspended`, `/accounts/disabled`, `/consent`) and Meta
  checkpoint/login errors fail immediately with non-retryable `LoginError`; `feedback_required`/
  `spam` fails with non-retryable `Blocked`; "Please wait a few minutes" answers are blocks.
  API `LoginError` and `Blocked` responses never fall back to `in_page=True`.
- **Known gaps:** story links are refused; only stories active now; a private post is `NotFound`,
  not `Hidden`; `tagged`, `stories`, `profile` are read in full on every run; once complete,
  `highlights` stops after 2 unchanged albums unless `--again`.
- **Saved as:** `media/ins/example_user/posts/2026-01-02_Sunset_at_the_beach_Cexample123_0.jpg`,
  `media/ins/example_user/highlights/Travel/2026-01-02_1234567890.mp4`

### thr (Threads)

- **Reads:** a profile's posts, replies, reposts and picture; one post by link: the page's embedded
  data, then its GraphQL feed scrolled to its end. No `me`, no search. Unconfirmed live.
- **Categories:** `posts` NF, `replies` NF, `profile` (own); `reposts` (opt-in). A repost carries
  the original post's date, so `reposts` is read to its end. Only a post of the profile's own is
  kept in `posts` and `replies`; a post without files is skipped.
- **Folder:** `{category}`; `profile`. A single post goes in its owner's folder, as a reply (its
  `reply_to_author` set) under `replies`, else `posts`.
- **Filename:** `{date}_{title:.40}_{post}{index}` (`title` is `headline` of the caption; empty,
  it leaves no `_`); a carousel or video post saves each file, a video as its best MP4.
- **Key:** `{post}_{num}` (`thr:C1abc_0`); `profile` keys by `{path}`.
- **Login:** cookie `sessionid`, as Instagram's (optional).
- **Limits and walls:** pace 2.0; walls `/login`, `/accounts/login`. Lock/challenge walls
  (`/challenge`, `/accounts/suspended`, `/accounts/disabled`, `/consent`) and Meta
  checkpoint/login errors fail immediately with non-retryable `LoginError`; `feedback_required`/
  `spam` fails with non-retryable `Blocked`; "Please wait a few minutes" answers are blocks.
  429s hold and reload once per progress (no-progress reloads abort).
- **Known gaps:** a post is opened by `/t/<code>`; only images and videos are saved.
- **Saved as:** `media/thr/example_user/posts/2026-01-02_Morning_walk_C1abc_0.jpg`

### twt (Twitter/X)

- **Reads:** a user's posts, Media tab (both Videos on `/media` and Photos on `/media?filter=photo`
  when X splits `/media`), highlights, reposts, replies, avatar and banner; one tweet; a search;
  the accounts you follow. Profile and tweets: GraphQL; timelines and searches: scrolled.
- **Categories:** `posts` NF, `media` NF, `highlights`, `replies` NF, `profile` (own); `reposts`
  (opt-in). Retweets are left out of every category but `reposts`; a tweet's files include the media
  of a tweet it quotes and its card image.
- **Folder:** `{part}` (the category; `media` uses `images` and `videos`); `profile`, under
  `media/twt/{name}_{target}/`: `name` is the author's screen name when the target is a user id
  (`twt:42`), else empty, so `twt:example_handle` stays `example_handle`.
- **Filename:** `{date}_{title:.40}_{post}_{n}` (`n` is `0`, `1`, ..., `q0` for a quoted tweet,
  `card`; `title` is `headline` of the tweet's text, a long tweet's own text when it has one;
  empty, it leaves no `_`; the profile pictures keep their names).
- **Key:** `{post}_{n}` (`twt:<tweet id>_<n>`); `profile` keys by its path under the folder.
- **Search:** `twt?<words>` opens the Latest tab; X operators (`from:`, `filter:media`) work;
  `--when` is sent as `since:`/`until:`. When X's cursor ends early or a 429 cuts a window, a
  `max_id:` window follows from the oldest of its last 5 tweets; a window with no new tweet ends
  it; 3 empty pages pass; a 429 holds that bucket. Each tweet with files is a `post` target in the
  search's `--folder`. Tagged: `twt?@example_handle -from:example_handle`.
- **Login:** cookie `auth_token` (a logged-out browser lands on the login wall); API calls send
  `ct0` back as the CSRF token.
- **Limits and walls:** pace 1.5 per bucket, CDN caps 4 (`pbs.twimg.com`, `video.twimg.com`),
  with humanized per-page, inter-tab, and search-window pauses. Low `x-rate-limit-remaining`
  headers pause on the open tab until `x-rate-limit-reset` and persist across runs; a 429 holds
  only its operation and aborts if a reload makes no progress. Login walls: `/i/flow/login`,
  `/login`; lock/challenge walls (`/account/access`, `/account/login_challenge`,
  `/i/flow/consent_flow`, `/i/flow/verify`), suspended accounts, and GraphQL codes
  `326`/`64`/`141`/`32`/`239` fail immediately with non-retryable `LoginError` (`226` fails with
  non-retryable `Blocked`; `88` holds 15m). Non-fatal GraphQL timeline errors click X's Retry
  button (up to 3 times with backoff), and empty pages with `stopOnEmptyResponse: false` keep
  scrolling. Sensitive-profile warnings are dismissed automatically across tabs and 429 reloads; a
  gated or protected profile with no visible timeline raises `Hidden`. A feed that stops before its
  cursor ends (or gets 0 responses or an unrecovered GraphQL error) is left incomplete rather than
  marked complete. For large profiles (`posts` reaching 400+ tweets), `posts` continues backward
  via resumable `max_id:` search backfill past X's profile timeline cap (skipped on incremental
  sync, protected accounts, or `[plugins.twt] backfill = false`), pausing safely after 2 search
  rate-limit resets to protect the account. Query ids are refreshed from x.com's script on a 400
  or 404, at most once a run.
- **Known gaps:** a single tweet has no browser fallback; a protected and a deleted tweet are both
  `NotFound`; video is MP4 only (streaming-only is skipped); text-only tweets save nothing.
- **Saved as:** `media/twt/example_handle/posts/2026-01-02_Morning_run_1234567890_0.jpg`,
  `media/twt/example_user_42/posts/2026-01-02_Morning_run_1234567890_0.jpg` (target `twt:42`)

### tik (TikTok)

- **Reads:** a profile in the browser, its lists scrolled to their end. One post from the embed
  page; a short link in the browser (see Targets).
- **Categories:** `videos` NF, `stories`, `playlists` NF, `profile` (own); `reposts`, `liked`,
  `favorites` (opt-in; the last two only if public). `videos` and `playlists` share one scroll.
- **Folder:** `{part}/{album}`: `part` is `videos` or `images` for a post, `albums` for a playlist
  video (`album` is the playlist's name), else the category; `profile` for `profile`.
- **Filename:** `{date}_{title:.40}_{post}_{idx}`: `title` is the post's caption through
  `headline`, empty (and dropped with its `_`) for no caption or only tags; `idx` is empty for a
  video, and a photo's place from 1.
- **Key:** `{post}_{idx}` (`tik:<id>`, `tik:<id>_<n>`); `profile` keys `profile/avatar_<id>.jpg`.
- **Login:** cookies `sessionid` or `sid_tt` (optional).
- **Limits and walls:** pace 1.5; no login walls (`/verify` and `/account/suspended` fail
  immediately with non-retryable `LoginError`, and status codes `8`/`2483` fail auth or
  `10000`/`10201` fail captcha). A captcha gets 60 s in the browser window (a hidden one
  is shown), then a block hold; a fourth unsolved one fails the job; a post list stopped short by
  one is read again (3 times at most). A 429 holds and reloads once per progress (re-waiting
  captchas; no-progress reloads abort); an empty page that says `hasMore` holds once, then ends
  with a warning. A walled post list fails `videos` and `playlists` with `Hidden`.
- **Known gaps:** no such tab is logged; a tab deaf to the click for 10 s fails `Changed`; stories
  only as the page shows them on load.
- **Saved as:**
  `media/tik/example_creator/videos/2026-01-02_Morning_run_1234567890123456789.mp4`,
  `media/tik/example_creator/albums/Example_Playlist/2026-01-03_1234567890123456781.mp4` (no
  caption)

### fbk (Facebook)

- **Reads:** a profile or page in the browser, and one video, reel, photo or post by link (a
  `fb.watch` or `/share/` link is opened to find where it lands). A tab is scrolled until its
  `page_info` says it ends; nothing is replayed.
- **Categories:** `posts` NF, `photos` NF, `reels` NF, `videos` NF, `highlights`, `albums`,
  `stories`, `profile` (own); `tagged` (opt-in). A video found in another tab goes to `videos`, a
  photo naming its album to `albums/<album>`; others' media (including shared posts) and sidebar
  preview tiles are left out.
- **Folder:** `{part}/{album}`; `highlights/{album}` for `highlights`; `profile` for `profile`.
  **Filename:** `{date}_{post}` (an undated photo takes its date from `posts` or HTTP
  `Last-Modified`); profile pictures `profile_<id>`, covers `cover_<id>`.
- **Key:** `{post}` (`fbk:<id>`, so a photo in two tabs is saved once); `highlights` and `profile`
  key `{path}`.
- **Login:** cookies `c_user` or `xs` (optional, but a logged-out browser sees little).
- **Limits and walls:** pace 1.5; walls `/login`, `/login.php`. Checkpoint/lock walls
  (`/checkpoint`, `/checkpoint/block`, `/two_step_verification`, `/account_disabled`, `/suspended`)
  and fatal GraphQL auth codes (`190`, `459`, `1346003`, `1357001`, `1357004`) fail immediately
  with non-retryable `LoginError`. A 429 holds and reloads once per progress (no-progress reloads
  abort); a "temporarily blocked" page, Comet's error line and GraphQL `errors` codes 1675004 and
  368 (confirm live) hold, then carry on from the same cursor. The CDN needs
  `Referer: https://www.facebook.com/`.
- **Known gaps:** `/photos /groups /events /stories /marketplace /hashtag` are refused; a page is
  told from a profile by the header's `__typename` (confirm live); stories are those active now;
  without a header `stories` and `profile` are skipped; undated photos take a neighbour's date.
- **Saved as:** `media/fbk/example.page/photos/2026-01-02_1234567890.jpg`,
  `media/fbk/example.page/albums/Holiday/2026-01-02_1234567891.jpg`

### loi (LOIBUS)

- **Reads:** one post: `info.md` (its fields, description, tags, links), previews and, for a VIP
  account, archives. HTTP to `api.moegoat.com`, signed as the site's script signs; links are AES.
- **Categories:** `archive` NF (info.md with links, the archives) then `photo` NF (previews; info.md
  without links only when `archive` is not wanted), both own.
- **Root:** `{author}`. **Folder:** `{post} {title}`, under `media/loi/<author>/` (`author` is the
  post's category, `misc` when none); previews (`01.jpg`), info.md and an archive's files all sit
  in it, the archive is unpacked there (`unpack="here"`) and deleted. **Key:**
  `{dir}/{part}/{file}` (`part` is `images` for previews): the path from before previews left
  `images/`, so saved posts stay saved.
- **Login:** cookie `access-token` (optional; a Bearer token). Archives need a VIP account; without
  one `archive` warns and writes info.md without links.
- **Limits and walls:** pace 0.5. Daily archive quota by VIP level: 15, 25, 40, 60 (SVIP doubles),
  read once per run, counted per UTC day in the plugin's state; once it is used up `archive` skips
  the day's posts with a warning, and the run says how many were left for tomorrow. A 429 holds for
  its `Retry-After`; a JSON rate answer is a block hold.
- **Author:** `loi:author/<id>` reads
  `GET api/user/library/loier/id?category_id=<id>&sort=latest&page=<n>`: each page's `data` ids as
  `loi:<id>`, ended by `pagination.total_pages` or a short page; rate answers are held like
  the links'.
- **Known gaps:** no tag target; a post counts once its links are fetched; Baidu links go to
  info.md only; an expired token reads as "no download links".
- **Saved as:** `media/loi/example_author/1234567890_Example_Title/info.md`

### mht (mimihentai)

- **Reads:** the site's public JSON API over HTTP; no login, no browser. A manga's chapters (pages
  numbered `001`, `002`, ...) and its cover; `author`, `genre`, `parody`, `character` and `query`
  find manga and each is got as its own `mht:<id>` target. Authors, genres and parodies match
  ignoring case and accents.
- **Categories:** `chapters`, `cover` (own). Not newest first: every chapter is listed, and the
  engine skips saved ones. A chapter is single; the finders pass categories on.
- **Folder:** `chapters/{order} {title}` (`order` zero-padded to 3 digits when whole); `cover` for
  `cover`; all under `media/mht/{name}_{target}/`, `name` the manga's title (a chapter link's data
  has none, so it files under its manga's kept folder, else under the id alone).
  **Filename:** the plugin's (`001`, `cover`; the engine adds the extension). **Key:**
  `{post}_{num}` (`mht:<chapter id>_<n>`); `cover` keys `{post}_cover`.
- **Login:** none: `flnd auth login mht` is `mht needs no login`.
- **Limits and walls:** pace 0.5; no walls. Searches page by cursor and stop on a missing, empty or
  repeated cursor.
- **Known gaps:** `chapters:<n>` is an exact count, checked on each result; negation works for
  genres only; `mht:<number>` is always a manga.
- **Saved as:** `media/mht/Example_Title_123/chapters/001_Example_Chapter/001.jpg`

### smp (SimpCity)

- **Reads:** a forum thread's posts over HTTP with the login's cookies, newest page first: the
  first read asks `/threads/<id>/latest` (its redirect names the last page), a sync starts at the
  last page the plugin's state saved (`thread:<id>`), goes through `latest` when that page has a
  newer one, then reads page by page backward. A post is `article.message[data-content]`, dated by
  `time.u-dt[data-timestamp]`; `.message-signature`, `blockquote.bbCodeBlock--quote` and
  `.bbCodeBlock--unfurl` are left out. Outbound links (`/<x>/?to=`, `url=`, `u=`, base64 or plain)
  are decoded here and never requested. No target is ever yielded; links to other sites are not
  collected yet.
- **Categories:** `images`, `videos` (own, NF). Images: goonbox links (`/img/<id>`, album
  `/a/<id>` through `/api/albums/<id>/images`) and thumbnails on `*.cuckcapital.cr`,
  `*.host.church`, `*.jpg5.su` (`.md.`, `.th.`, `.fr.` taken out of the name). Videos: turbo
  (`/embed/`, `/v/`, `/d/`; `/api/sign?v=<id>`, signed and lazy, so `refresh` lists it again).
  A post's files are listed lazily; each link counts once per post; a removed or changed file is
  warned about and left out, and the post fails only when nothing is left.
- **Folder:** `{category}`, under `media/smp/<thread id>/`. **Filename:** `{date}_{post}_{num}`, or
  `{date}_{post}_{original name}` when the host gives one. **Key:** `{post}_{num}`.
- **Login:** a cookie whose name matches `.+_user`; required.
- **Limits and walls:** pace 5 s plus up to 7 s of jitter (5 to 12 s) before each forum page, one
  page at a time; a 45 to 90 s break every 25 pages; at most `pages_per_hour` (default 300,
  `[plugins.smp]`) forum pages an hour, counted in the plugin's state, then it pauses until the
  hour turns (`smp: pausing until 14:00 UTC, hourly page budget`). `Referer` is the thread's
  previous page. DDoS-Guard's 403 (`server: ddos-guard`) is a block, held 30 s, 2 min, 8 min;
  `/login` and `data-logged-in="false"` are the login wall. Only http and https links to a name
  (never an IP address, `localhost` or a local name) are used; a host's API answer must come from
  its own domain; an HTML answer where a file was expected fails that file.
- **Captcha:** a page that lands on `/captcha` opens a browser tab (`browser` is `any`; none
  opens otherwise) and asks a person to solve it, up to 10 minutes; one target at a time, the
  others wait. Then the request is made again over HTTP and this hour's page budget is halved. If
  nobody solves it, or it comes back at once, every smp target of the run fails with `smp:
  simpcity.cr wants a captcha` (hint `open simpcity.cr in your browser, solve it, then run again`)
  and is never retried.
- **Known gaps:** a post edited later isn't seen by a sync; a cut-short first download reads its
  pages again from the newest; the same file in two posts of a thread is saved twice; no post
  route, `links`, `hosted` category, attachments or file hosts but goonbox and turbo yet; host API
  shapes are unverified live.
- **Saved as:** `media/smp/123/images/2026-01-01_456_name.jpg`

### pxv (pixiv)

- **Reads:** pixiv's web JSON API over HTTP, `www.pixiv.net/ajax/...`; a browser opens only for
  Cloudflare's check (see Captcha). A user's works:
  `GET /ajax/user/<id>/profile/all` once for the run (`illusts` and `manga` are objects keyed by
  work id, or `[]` when empty), the ids sorted as numbers, newest first, then
  `/profile/illusts?ids[]=...&work_category=illustManga&is_first_page=0`, 100 ids a request
  (`body.works` by id); both are kept for the run and shared by the categories. A post's files
  are lazy: `/ajax/illust/<id>/pages`, `body[].urls.original`. A date is `createDate` as Unix
  seconds. A single artwork: `/ajax/illust/<id>`, whose `userId` owns it and whose `illustType`
  gives its category (0 `illusts`, 1 `manga`, 2 `ugoira`); a one-page work uses
  `urls.original`, any other `/pages`. An ugoira's files: `/ajax/illust/<id>/ugoira_meta`, whose
  `originalSrc` is the full-size zip (`src`, the small one, with a warning when it is missing;
  neither is `Changed`) and `frames[] = {file, delay}` (milliseconds). A user's novels: the
  `novels` list of `profile/all` (an object keyed by id, or `[]`), then
  `/profile/novels?ids[]=...`, 100 a request (`body.works` by id), kept and shared like the
  others. A novel's files: `/ajax/novel/<id>` (`content`, `title`, `createDate`, `userId`,
  `description`, `tags.tags[].tag`, `seriesNavData`, `coverUrl`, `textEmbeddedImages`), and for
  `[pixivimage:...]` one `/ajax/novel/<id>/insert_illusts?id[]=<illust>-<page>&...` for all of
  them (`body[<illust>-<page>].illust.images.original`, `visible`). A single novel asks
  `/ajax/novel/<id>` once, files it by its `userId` under `novels`, and reuses the answer.
  `sign` adds `Referer: https://www.pixiv.net/` and `Accept:
  application/json` to `/ajax/` requests, and each post carries the Referer for its images.
- **Search:** `pxv?<words>` (and the tag links, their words percent-decoded) discovers the
  artworks of a tag, newest first: `GET /ajax/search/artworks/<words>?word=<words>&order=date_d
  &mode=<all, or safe when logged out>&s_mode=s_tag&type=all&p=<n>`, with `scd=<since>&
  ecd=<until>` (ISO days) from `--when`. A page is 60 hits in `body.illustManga.data[]`, and
  `body.illustManga.lastPage` is the last page's number. Each hit's `id` is yielded as the
  target `pxv:artwork/<id>`; a hit with no numeric id, or one already seen, is skipped. It stops
  at a page with nothing new (an empty page) or after `lastPage`. `s_mode=s_tag` matches part
  of a tag. Novels aren't searched. No words is `pxv: a search needs words: pxv?example_tag`.
  The search asks the API like any request, so it keeps the pace, the budget and the captcha
  handling.
- **Categories:** `illusts` (`illustType` 0), `manga` (1), `ugoira` (2) and `novels`, all own and
  newest first; `illusts` and `ugoira` share the profile's illust id list and batches. With a sync
  head (`ctx.head`), once a whole batch's newest `createDate` is strictly older than the head's
  time, no more batches are asked for, so a caught-up profile costs 2 requests; `novels` stops
  the same way. `profile` is own and not newest first, `folder="profile"`, `key="{path}"`:
  `GET /ajax/user/<id>?full=1` (asked once a run, and shared with the bookmarks' name) gives
  `imageBig`, the avatar, whose original is its path without the `_<n>` before the extension
  (a default picture, which isn't a `user-profile` image, is skipped), and `background.url`
  (null when there is none), whose original has the `/c/<size>/` crop part removed. They are
  one post of the user's id, named `avatar_<YYYY-MM-DD>` and `background_<YYYY-MM-DD>` from the
  day in the CDN path (`/img/YYYY/MM/DD/`), extension kept, so a new picture lands beside the
  old. `shiori` (the artworks the user bookmarked) and `novelshiori` (the novels) are
  opt-in and not newest first: a bookmark carries its work's date, not its own, so each is read
  to its end on every run. `GET /ajax/user/<id>/illusts/bookmarks?tag=&offset=<n>&limit=48&
  rest=show` (`novels/bookmarks`, `limit=24`) goes through `ctx.pages` with the offset as the
  cursor, so a cut-short read resumes; a page is `body.works[]` (as in a batch) and
  `body.total`. When the target is the login's own account (the number before `_` in the
  `PHPSESSID` value is the user id), `rest=hide` is read after `rest=show`, for the private
  bookmarks. A deleted or masked item (`isMasked`, id `0`, no title) or an unknown `illustType`
  is skipped with a debug line. Files are those of the work's type, keyed `{post}_{num}`
  (`novel_{post}_{num}` for novels), the same as the artist's own, so a work saved twice is
  saved once. Each page counts against the hourly budget, and a Cloudflare check is solved as
  for any request.
- **Root:** `{name}_{target}`: `name` is the display name of the account that owns the target (a
  profile's user for each of its categories; a single work's owner), not the work's artist, who
  stays in `user`; `target` is the user id. An empty name gives the id alone. The name comes from
  `userName` of the works the profile lists (each is the profile user's own) or of the single
  work; the engine keeps the root the first save gave, so a rename on pixiv never moves a folder
  (see [`root`](#root)). For `shiori`, `novelshiori` and `profile` it is the account's own,
  from `GET /ajax/user/<id>?full=1` (`body.name`) once a run; the other categories don't ask.
- **Folder:** `{category}`, under `media/pxv/<name>_<user id>/`. **Filename:**
  `{date}_{title:.40}_{post}_p{num}`
  (`title` is the work's own title; `p<num>` is pixiv's own page number); a novel's files set
  their own names (see Novels).
- **Ugoira:** one `File.frames(originalSrc, frames, keep=...)`, so the video is
  `<date>_<title>_<post>_p0.mp4`. It needs ffmpeg, as DASH does (a missing one is a
  `ToolError`). Setting
  `ugoira` under `[plugins.pxv]`: `"both"` (the default) keeps the zip beside the video as
  `<date>_<title>_<post>_p0.zip` and adds `<date>_<title>_<post>_p0.json` (`File.text`:
  `mime_type` and the
  `frames` as pixiv gave them); `"mp4"` saves the video alone. Any other value warns once and
  is read as `"both"`. An undated post has no `<date>_`. The `.json`'s name is made by the
  plugin with the engine's own rules (the title cut to 40 characters, then the name cleaned as
  `flnkit.text.filename` cleans), and a test checks it against the engine's.
- **Novels:** a post's files are the text `<date>_<title>_<post>.md` (`File.text`), the cover,
  then each
  image. The `.md` starts with `# <title>`, then lines `- Author: https://www.pixiv.net/users/<id>`,
  `- Date: <day>`, `- Series: <title>, no. <order>` (only with a series), `- Tags: a, b` (only
  with tags) and the caption (`<br>` as newlines, other tags dropped), then `---` and the text.
  Conversion: `[chapter:<t>]` is `## <t>`; `[newpage]` is `---` with a blank line each side;
  `[[rb:<base> > <ruby>]]` is
  `<ruby>base<rt>ruby</rt></ruby>`; `[[jumpuri:<text> > <url>]]` is `[text](url)` for an http or
  https URL with no space or parenthesis, else the text alone; `[jump:<n>]` is `(page <n>)`;
  `[uploadedimage:<id>]` (from `textEmbeddedImages`) and `[pixivimage:<illust>-<page>]` (`<illust>`
  alone is page 1) are `![](<image name>)`. Anything else stays as written, and so does an image
  that is not visible or has no original, with a `warn` log. An image is
  `<date>_<title>_<post>_<n><extension of its URL>`, `n` from 1 in text order over the images that
  resolved, each distinct image once. A cover that is an `i.pximg.net` `novel-cover-master` image
  is saved as its original (the `/c/<size>/` part removed, `novel-cover-original`,
  `_master1200` dropped) with `File(mirrors=[.jpg, .png])`,
  named `<date>_<title>_<post>_cover`; any other
  cover (pixiv's default image) is not saved. An undated novel has no `<date>_`, and one with no
  title has no `<title>_`. The title is cut to 40 characters and cleaned as the engine cleans
  a name, so the engine leaves every name as the plugin set it; in the Markdown a name's
  `( ) [ ] < > # %` and spaces are percent-escaped. A novel's answer
  with no `content` is `Changed`.
- **Key:** `{post}_{num}`; a novel's is `novel_{post}_{num}`, since artwork and novel ids are two
  number series. **Fields:** `title`, `user` (the work's artist's display name), `name` (the
  account's, above), never in the defaults.
- **Login:** `PHPSESSID`, optional. Logged out, R-18 works are hidden.
- **Errors:** read from the status, `error` and the body's shape, never the message text. 404, or
  `error` on a 200, is `NotFound`; 400 or 403 with `error` is `Hidden` (with the hint `log in:
  flnd auth login pxv <file>` when logged out), not a login error; 401 is `LoginError`; 429 is a
  block held for `Retry-After` when given; Cloudflare's check is not an error answer but the
  captcha step below; any other HTML, or no `body`, is `Changed`. A work with `isMasked`, or a
  thumbnail on `s.pximg.net/common/images/limit_...`, is
  `Hidden`, only that post: `sanity_level` hints `log in, and turn on R-18 (and R-18G) in pixiv's
  settings: Viewing restrictions`, `mypixiv` hints `only the artist's My pixiv friends can see it`,
  `unviewable` has no hint.
- **Limits and walls:** pace 2 s plus up to 2 s of jitter (2 to 4 s) before each API request; a 30
  to 60 s break every 100 API requests; at most `requests_per_hour` (default 900, `[plugins.pxv]`)
  API requests an hour, counted in the plugin's state, then it pauses until the hour turns (`pxv:
  pausing until 14:00 UTC, hourly request budget`). Images: 4 at once from `i.pximg.net`.
- **Captcha:** an answer from pixiv.net is Cloudflare's check when it is a 403 with
  `cf-mitigated: challenge`, or HTML holding `<title>Just a moment...</title>` or
  `challenge-platform`. It opens `https://www.pixiv.net/` in a browser tab (`browser` is `any`;
  none opens otherwise) and, if the tab shows the check, asks a person to solve it, up to 10
  minutes; one target at a time, the others wait and reuse that solve. Then this hour's request
  budget is halved (at least 1) and the request is made again over HTTP, with the browser's
  cookies copied into the session. If that is checked again, every later API request of the run is
  sent from the solved tab (`in_page`) and the request is made once more that way. If nobody
  solves it, or the tab's request is checked too, every pxv target of the run fails with `pxv:
  pixiv.net wants a captcha` (hint `open www.pixiv.net in your browser, solve it, then run again`)
  and is never retried. Images from `i.pximg.net` are never checked.
- **Known gaps:** not built yet: `profile`, `shiori` and
  `novelshiori` categories and tag search. A novel's series is shown in its header only; series
  are not targets. A work edited later isn't seen by a
  sync; a user's private works are never listed. Answer shapes beyond the live checks are
  unverified.
- **Saved as:** `media/pxv/example_user_12345678/illusts/2026-01-02_Sunset_123456789_p0.png`,
  `media/pxv/example_user_12345678/ugoira/2026-01-02_Waves_123456790_p0.mp4`
  (with `.zip` and `.json`),
  `media/pxv/example_user_12345678/novels/2026-01-02_Example_Story_23456789.md`
  (with `_cover.jpg` and `_1.png`),
  `media/pxv/example_user_12345678/profile/avatar_2026-01-02.jpg`

### web (the web)

- **Reads:** a link no other plugin owns: a file as it is, an HLS (`.m3u8`) or DASH (`.mpd`) stream,
  or the media a page shows. By extension first; else a `HEAD` decides by `Content-Type`; a server
  that refuses `HEAD` gets a one-byte ranged `GET`. A page is read for `og:video`, `<video src>` and
  `<source>`, then `og:image`, each once.
- **Categories:** none; every link is one post.
- **Folder:** `{category}` (a link's files sit in its target folder). **Filename:** `{name}`, the
  link's last path part without extension, else the host; a second file takes `_2`. **Key:**
  `{post}_{num}` (`web:<link>_<n>`; the link without fragment or `user:pw@`).
- **Login:** none; owns no domain; sends no cookies. A stream is saved as `.mp4`.
- **Limits and walls:** pace 0.5; each file is fetched with the link as its `Referer`.
- **Known gaps:** no browser, so media built by scripts shows none; keyed streams (AES, DRM) cannot
  be read; only Open Graph tags and `<video>` are read; files are undated; a page may point at a
  private address and it is fetched.

## For developers

Rules for changing the code are in `AGENTS.md`. The shape:

```text
flnkit/   standalone toolkit, imports nothing of flnd: net browser files html dig text clock
          db cookies media stream errors, and ui/: theme out forms
src/      the package flnd
  _library.py cli.py errors.py plugin.py
  engine/  run jobs crawl save resolve plugins auth store config
  ui/      screen log (imports only flnkit, ui, events, errors)
  plugins/ the built-ins, importing only flnd.plugin and the standard library
```

`tests/test_layers.py` checks the import rules. A run has one asyncio loop on its own thread; plugin
code is async; only `engine/jobs.py` retries, sleeps and picks threads; a commit is one per post.

**The home's files.** `flnd.db` is SQLite in WAL mode, schema version 5: `files` (a row per file,
key `<code>:<key>`, with target, post, category, path, size, date, state `saved`, `gone` or
`link`), `posts` (a view of `files`), `targets` (follow, categories, cursors, heads, folder),
`target_seen` (the targets a search found), `runs`, `failures` (class, message, hint, attempts,
redacted URL), `state` (a plugin's own values). One transaction per post; a file's row is written
after the file, so a crash leaves at worst a file without a row. A file
listed in two categories of a target is saved once, and the second is a link row `<key>@<category>`.
Read it with any SQLite tool.

**make targets**, run inside `flnd/`:

| Target | Does |
| --- | --- |
| `make check` | ruff (`make lint` alone), then every test; green at every commit |
| `make test ARGS="-k fetch"` | the tests, or some (`FLND_TEST_NO_PROFILES=1`) |
| `make bench` | offline benchmarks; `ARGS=--gate` fails on a 15% slowdown, `--record` rebases |
| `make setup`, `clean` | sync `.venv`; remove builds |
| `make try ARGS="ins:example_user"` | list, live, what a get would save (scratch home `.try`) |
| `make live`, `make test-browser` | hand checks, never part of `check` |

**Benchmarks** (`tests/bench/`, groups `startup ui net disk db walk cpu e2e browser`) run offline
against a loopback server and scratch folders; `make check` runs each in smoke mode only. Run them
before a speed change, and `--gate` before its commit. The `browser` group also reports `heavy_*`
and `lean_*` (Chrome's peak MiB and CPU seconds on a page of 300 images), via `ps`, not on Windows.

flnd lowers its own priority at the start of `get` and `sync` (`os.nice`, or `SetPriorityClass` on
Windows), and its child processes, the browser's among them, inherit it. On Windows that
inheritance is unverified.

### Adding a built-in plugin

A built-in is one module of `src/plugins/`, named by its code, that imports only `flnd.plugin` and
the standard library (its SDK inside a function). Add a row to `TABLE` in `src/plugins/__init__.py`
(code, module, class, domains), so `--help` and the link resolver find it without importing it.
Its tests run on `tests/plugin_kit.py`: `FakeCtx` stands in for the run's context, `FakePage` and
`Reply` for pages and answers, and `collect` runs a route to its posts (`FakeCtx(head=...)` fakes
`ctx.head`). Then give it a section under
[Plugins](#plugins), headed by its code.
