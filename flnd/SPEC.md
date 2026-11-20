# flnd spec

Everything about flnd on one page: what each command takes, what each site's plugin reads, what each
refusal says, and how to script it. The words, once: a **target** is what you ask for
(`ins:example_user`), a **plugin** is the code for one site, a **route** is the part of a plugin
that reads one kind of target, a **category** is one part of a target (`reels`), a **post** is one
item of a site with its **files**, and the **home** is the folder where flnd keeps all of it.

Sections: [Install and first run](#install-and-first-run) · [Targets](#targets) · [Dates](#dates) ·
[Categories](#categories) · [Folders](#folders) · [Sync](#sync) · [Logins and
accounts](#logins-and-accounts) · [When it goes wrong](#when-it-goes-wrong) · [Commands and
flags](#commands-and-flags) · [Config](#config) · [Plugins](#plugins) · [Python](#python) · [Writing
a plugin](#writing-a-plugin) · [For developers](#for-developers)

## Install and first run

flnd needs [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer.

```sh
git clone https://github.com/feiluvnana/flntools
cd flntools
uv tool install -e ./flnd        # puts `flnd` on your PATH, Telegram support included
```

From a checkout, `make setup` (inside `flnd/`) makes `.venv`, then `uv run flnd ...` runs it. On
Windows, `winget install ezwinports.make`, or type the `uv` command each target holds.

Outside programs, each needed only for some files:

- Google Chrome, for the sites that open a browser. When missing: asks once to download Chromium
  (about 200 MB) on a terminal; off a terminal the run stops with "Google Chrome is needed".
- ffmpeg, for joining a video with its sound track. When missing: a `ToolError`; that file fails and
  is retried on later runs.
- 7-Zip (`7zz`, or `7z`), for 7z, RAR and encrypted zip archives. When missing: a `ToolError`; the
  archive is kept, not extracted.

zip and tar are read by Python. flnd finds each program on your PATH, never downloads one.

First run:

```sh
flnd auth login ins chrome        # use your Chrome login for Instagram
flnd get ins:example_user         # its own content: posts, reels, stories, ...
flnd status                       # the last run, its failures, accounts, the disk
```

Then `flnd get` alone gets what is new for every target you have followed.

### The home

The home is `--home DIR`, else `$FLND_HOME`, else `./.flnd`. Nothing is written until the first
command that saves something.

```text
.flnd/
  config.toml           settings, optional ([defaults] and [plugins.<code>])
  metadata/flnd.db      files, targets, runs, failures, accounts (SQLite)
  metadata/secret.db    the vault: logins and cookies, sealed under a key outside the home
  media/<code>/<value>/<folder>/...     the downloaded files of each target
  log/<time>_get.jsonl  the run journal; log/crash-<time>.txt a bug's traceback
```

Never share a home with someone you wouldn't give your logins to. `log/` is swept at the start of a
run that downloads: entries older than 30 days go.

| Variable | Used for |
| --- | --- |
| `FLND_HOME` | the home when `--home` is not given |
| `FLND_PLUGINS` | the folder installed plugins load from (default `~/.config/flnd/plugins/`) |
| `NO_COLOR`, `TERM=dumb` | turn colour off (so does a pipe or a file) |
| `LANG`, `LC_ALL` | a terminal that is not UTF-8 gets `v x * - >` for the glyphs |
| `APPDATA` | on Windows, the user folder (`%APPDATA%\flnd\`) instead of `~/.config/flnd/` |

A setting resolves in this order: a flag or library argument for one call, the home's `config.toml`,
the user file (`~/.config/flnd/config.toml`), the default.

## Targets

A target names what to download. Its whole grammar:

```text
target     = text | link
text       = <code>:<value>[|<categories>]      a user, a post, a chat ...
           | <code>?<query>[|<categories>]      a search
link       = http(s)://<host>/<path>[?<query>][|<categories>]   a page on a site
           | <host>/<path>[|<categories>]       when some plugin owns <host>
value      = <name> | me | <kind>/<id>          a user, the accounts you follow, one post
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
   route that reads any text (a Telegram title) splits only when every word after the `|` is a
   category or `all`, so `tlg:Fans|Lens` stays whole. A one-post route given a list is
   `<code>:<kind>/<id> is one post, with no categories`. The old written form,
   `<code>:<value>/<categories>`, is refused with the hint `write the categories after |:
   "ins:example_user|tagged"`.
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
- `nope:x`: `no plugin named nope`; hint `Plugins: tik, fbk, ins, thr, twt, loi, tlg, mht, web`
- `ins:`: `ins: write a value after ins:`; hint `Write it like <every form of the plugin>`
- `ins:%`: `ins: can't read '%'`; hint `Write it like <every form of the plugin>`
- `ins:example_user|nope`: `ins has no category nope`; hint `Categories: posts, reels, tagged,
  reposts, stories, highlights, profile`
- `ins:example_user/tagged`: `ins:example_user/tagged: categories aren't written after a /`; hint
  `write the categories after |: "ins:example_user|tagged"`
- `ins:reel/C1`, `ins:example_user/posts/`: `ins:reel/C1: reel isn't a post kind of ins; write
  ins:post/<id>, or categories after |: "ins:<user>|tagged"` (every single kind of the plugin is
  listed; a plugin with none gives only the `|` hint)
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
`--when`, `--limit` or `--folder` is a `UsageError` too ([Dates](#dates), [Folders](#folders)). The
library says `give at least one target` when a call ends with none.

### Reading from standard input

`flnd get` reads standard input when it is not a terminal: each line is a target, blank lines and
lines starting with `#` are skipped, and the lines come after the targets on the command line
(`cat targets.txt | flnd get`).

### `me`, searches and `--limit`

- `ins:me`, `tik:me` and `fbk:me` list every account the login follows (`fbk`: pages and people, not
  friends), each its own `user` target (`"ins:me|posts,stories"` passes the list on to each). They
  need a login. `tik:me` reads your Following list, `fbk:me` the `/me/following` page, `twt:me` X's
  `Following` operation (all unconfirmed live).
- A search (`twt?<words>`, `mht?<words>`, `tik?<words>`, `loi?<words>`, or a search link) finds
  targets, and each found target is got as its own. A search needs `--folder`, which puts all its
  hits in one place ([A search needs a folder](#a-search-needs-a-folder)). A followed search gets
  only the targets it had not found before.
- `--limit N` (at least 1) is at most N posts per target, or N targets per search or other
  discovering route (`ins:me`, `mht:author/...`, `mht:genre/...`). `--limit 0` is `limit must be at
  least 1`.

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
| `ins:me` | `me` | every account the login follows |

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
| `twt:me` | `me` | the accounts the login follows |

A handle is 1 to 15 letters, digits or `_` (digits of any length: an id); another is refused
(`twt: 'x.com' isn't a handle`), as are `x.com/search` without `q=`, lists, communities and spaces
(`twt: lists aren't supported; ...`), and, as `twt: that's not a profile`, `home`, `explore`,
`settings`, `notifications`, `messages`, `hashtag`, `i` (`i/web/status/<id>` and `i/user/<id>`
read), `intent`, `compose`, `login`; `x.com/` alone.

**tik**

- `tik:<user>`, `tiktok.com/@<user>`: the profile (route ``user``)
- `tik:post/<id>` (so does the old `tik:<id>`, 16 digits or more), `tiktok.com/@<user>/video/<id>`
  and `/photo/<id>`, `tiktok.com/video/<id>` and `/photo/<id>`: one post (route ``post``)
- `vm.tiktok.com/<code>`, `vt.tiktok.com/<code>`, `tiktok.com/t/<code>`: one post, link only (route
  ``short``). It is opened in the browser and read from the video it lands on; a landing that is no
  video or photo post is `NotFound` with the hint `check the link in a browser; only video and photo
  links work`.
- `tik?<words>`, `tiktok.com/search?q=<words>`, `tiktok.com/search/video?q=<words>`: a search (route
  ``search``). The Videos tab's `api/search/item/full/` is paged like a profile's, each hit found as
  `tik:post/<id>`, so `--limit N` is N posts. It needs `--folder`. Unconfirmed live.
- `tik:me`: the accounts the login follows, each as `tik:<uniqueId>` (route ``me``; needs a login,
  else `LoginError` with the hint `log in: flnd auth login tik`), read from `api/user/list/` after
  opening your Following list; unconfirmed live.

`tik:123` (under 16 digits) is a user. Refused with `tik: <what> aren't supported; give a user, a
post or a search`: `tiktok.com/tag/...` (hashtag pages), `/music/...` (sound pages),
`/discover/...`, `/live`, `/explore`. Also refused: `@@user`, any other `tiktok.com` link.

**fbk**

- `fbk:<user>`, `facebook.com/<user>`, `/@<user>`, `/profile.php?id=<id>`, `/people/.../<id>`: a
  profile or page, one route (``user``); a tab it lacks (`tagged`) logs `no such tab`
- `fbk:me`: the accounts the login follows, each `fbk:<name or id>` (``me``); no login is a
  `LoginError`, hint `log in: flnd auth login fbk`
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

**tlg**

Every form is route ``chat``:

- `tlg:@<username>` or `tlg:<username>` (4 to 32 characters, a letter first): a chat
- `tlg:<title or id>` (`tlg:Example Chat`, `tlg:-1001234567890`): a chat among yours
- `t.me/<username>`, `t.me/s/<username>`, `t.me/+<invite>`, `t.me/joinchat/<invite>`: a chat
- `t.me/c/<id>` (read as `-100<id>`): a private chat

A message number at the end of a link is ignored. Refused: `tlg:@abc` (a username under 4
characters), `t.me/` alone, `web.telegram.org` links: `tlg: can't read <text>`.

**loi**

| Form | Route | Reads |
| --- | --- | --- |
| `loi:post/<id>`, `loix.cc/lois/<id>`, `moegoat.com/lois/<id>` (digits) | `post` | one post |
| `loi?<words>`, `loix.cc/search?q=<words>`, `moegoat.com/search?q=<words>` | `search` | a search |

The old `loi:<id>` reads too. A search finds `loi:post/<id>` (`--limit N` is N posts) and needs
`--folder`; its endpoint and paging are unconfirmed live. Every other page of both domains is
refused: `that page isn't supported; give a post or a search` (`/library...`: `author and library
pages aren't supported; ...`). `loi:post/<id>|archive` and `|photo` are allowed.

**mht**

- `mht:<id>`, `mimihentai.moe/manga/<id>` (digits): one manga (route ``manga``)
- `mht:chapter/<id>`, `mimihentai.moe/manga/.../chapter/<id>`: one chapter (route ``chapter``)
- `mht:author/<name or id>`, `mimihentai.moe/author/<x>`, `/authors/<x>`: an author's manga (route
  ``author``); `mht:genre/<x>`, `/genre/<x>`, `/genres/<x>`: a genre's (``genre``)
- `mht?<query>`: every manga a search finds (route ``query``)

Query terms: plain words (title), `title:`, `author:`, `genre:` (repeatable), `-genre:`, `parody:`,
`character:`, `chapters:<n>`. Quote values with spaces. Any other term, a `-` before a term but
`genre`, a term with no value, a `chapters:` that is not a number, and an empty query are refused.
`mht:chapter/5|chapters` is `mht:chapter/5 is one post, with no categories`.

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
- `twt` searches also send `--when` to X as `since:` and `until:`; `tlg` starts its listing after
  the last day.

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
| `fbk` | `posts photos albums reels videos stories profile` | `tagged` |
| `tlg` | `photo video file` | none |
| `loi` | `archive photo` | none |
| `mht` | `chapters cover` | none |
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
- The old `ins:example_user/tagged` is refused, with the hint to write it `|tagged`.
- A single post has no categories (naming one is `<code>:<kind>/<id> is one post, with no
  categories`); `loi:post/<id>` is the exception.
- `"ins:me|<list>"`, `"mht:author/<x>|<list>"`, `"mht?<q>|<list>"` and `"twt?<q>|<list>"` pass the
  list on to each target found. With no list, each takes its own.
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
saved once, and the second gets a link ([For developers](#for-developers)).

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

The owner is the post's: `user.username`, `screen_name`, `uniqueId`, a Facebook vanity name else its
id. With no owner in the data the post goes where it always did, `media/<code>/<post id>/`, with no
category folder, and a log line says so. `loi`'s template starts with `{author}`, so its root is
`media/loi/`. A search's hits ignore all this: they go to the search's `--folder` ([A search needs a
folder](#a-search-needs-a-folder)). Files saved earlier stay where they are, and keys are unchanged,
so a post saved by link is skipped, and found in place, when its profile is synced later.

### `--folder`

`--folder VALUE` (library: `folder=`) replaces the folder templates, the plugin's, route's,
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
| `query` | a search's words; empty for any other target |
| `path` | accepted, but only filled in `key` templates; empty in a folder |
| plugin fields | each plugin's `fields`, listed below the table |

Plugin fields: `ins` `album`; `twt` `part n`; `tik` `part album idx`; `fbk` `part album`; `tlg`
`chat chat_id`; `loi` `author title part`; `mht` `title order author genres chapters`; `web` `name`;
a plugin of your own declares its `fields`.

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
  category, count, date, extension, index, num, path, plugin, post, query, target, not this text`

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
- In the library, `folder=` also takes a mapping, `{"posts": "{target}/feed", "*": "..."}`; a
  template in it can't hold a comma outside `{}`.

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
(library: `folder=`) is given and places every category: one template, or a list with `*`.
`config.toml`'s `folder` doesn't count. A search's hits all go there, not to their owners' folders.

- `flnd get "twt?cats"`: `twt?cats: a search needs --folder`; hint `add --folder "{query}"`
- `--folder "posts:{query}/posts"`: `twt?cats: a search needs --folder to place every category`;
  hint `end the list with *:<template>, as in --folder "posts:{query}/posts,*:{query}/other"`

```sh
flnd get "twt?cats" --folder="{query}"
flnd get "twt?cats" --folder="posts:{query}/posts,*:{query}/other"
```

A followed search keeps its folder in its row, and `flnd get` with no target reuses it, as does
naming the search again without a `--folder`. Giving it another `--folder` is refused unless
`--again` is also given, which keeps the new one (files already saved stay where they are):

```text
twt?cats: this search is saved under --folder '{query}', not 'other'
hint: add --again to use the new folder; files already saved stay where they are
```

A followed search from before it kept a folder has none, and syncs as it did. `ins:me` and
`mht:author/...` are not searches, and need no folder.

### Names and keys in `config.toml`

```toml
[plugins.twt]
folder = "{part}"                  # the plugin's own default, here
filename = "{post}_{n}"            # drops the leading date
```

`filename` (no extension: the engine adds it) and `folder` replace the plugin's templates, and take
the same fields and checks. A category's own `folder` still wins over the plugin's. The history key
(`key`) is the plugin's and cannot be set.

### Parts, dedupe, sidecars

- Every file is written as `<name>.part` and renamed once whole, so a real name is a whole file. A
  stopped download resumes from its part (`Range`, with an `If-Range` validator). Parts nothing
  touched for 7 days are swept when a run starts on the target.
- `[defaults] dedupe = true` (default): a file whose bytes a saved file has becomes a hard link to
  it. A file system that cannot link keeps both copies and warns once.
- `[defaults] sidecars = true` writes `<file>.json` beside each file, with its post's source,
  target, post, category, date, url, caption, author, tags and the file's URL without its query.
- Archives (`.zip .7z .rar .tar*`) are extracted beside themselves into a folder of their name, and
  the archive stays.

## Sync

`flnd get` follows each target that is not a single post, so `flnd get` alone keeps them current.
What a run skips and where it stops:

- A saved file: skipped: its row says saved and the file is there, or the file is on disk at its
  planned path
- A gone file: a 404 or 410 records it `gone`; it is not asked for again unless `--again`
- Failed files: each `get` of a target first retries that target's failed files, on at most 3 later
  runs per file; a post is listed again from its page's cursor for fresh links
- Nothing new: a newest-first category keeps its head; a sync stops after the head and the post
  after it, one request when the site pages by cursor (`Skipped`, reason `nothing new`)
- Caught up: after 5 saved posts in a row (`[plugins.<code>] stop_after`)
- Older than `--when`: after 5 posts in a row older than its start
- A cut-short run: a full download keeps its cursor and resumes there; a cut sync keeps a gap and
  the next sync jumps to it once it meets saved posts
- Not newest first: read to the end on every run

`--again` lists every post again, past the head and saved cursors, and asks again for gone files.
Files saved on disk are still skipped, so it downloads only what is missing. Use it when a creator
unarchives an old post or a missing file comes back.

`--dry` saves and writes nothing: it prints `found <target>` for each target a search finds, then
`new <path>` or `saved <path>` for each file, and a count.

`--exec CMD` runs CMD once for each saved file, one at a time, without a shell: CMD is split once
like a POSIX shell (`shlex`) and run directly, and a file name from a site only fills a placeholder
inside one argument.

| Placeholder | Environment | Value |
| --- | --- | --- |
| `{path}` | `FLND_PATH` | the file's final path |
| `{target}` | `FLND_TARGET` | the target it was saved for, `ins:example_user` |
| `{post}` | `FLND_POST` | the post's id |

```sh
flnd get ins:example_user --exec 'cp {path} /Volumes/backup/'
flnd get --exec 'sh -c "echo \"$FLND_PATH\" >> saved.txt"'
```

To use a shell's features call the shell yourself and read the environment. A mistake in CMD
(`--exec: <error>: '<cmd>'`, `--exec needs a command`, `--exec: <program>: no such program`) is a
usage error before anything is saved. The program's standard output is dropped, never kept; its
standard error is read as it comes and only the last 4 KiB is kept, for the warning. A program that
cannot start, exits non-zero (the warning shows the last line it wrote to standard error) or runs
past `[defaults] exec_timeout` is a warning, and the run goes on. The program runs in its own
process group, and the whole group is killed on the timeout and when the run stops (Ctrl-C).
`[defaults] exec` in the user file runs a command for every get the same way; with both, `--exec`
wins.

Ctrl-C: the first stops listing and lets files in progress finish; the second stops at once and
leaves `.part` files to resume; the third quits at once, exit 130.

Two runs may share a home: a part is its own lock, and a file another run is saving is skipped as
`busy`.

## Logins and accounts

`flnd auth login <plugin> [method] [path]` logs an account in. A method is one of the plugin's own
(`flnd auth login <plugin>` with none, on a terminal, shows a menu):

- `browser`: opens flnd's Chromium at the site's login page; log in, and the window closes once a
  login cookie is set (waits up to 10 minutes; closing it gives up)
- `chrome`: reads the site's cookies from your Chrome through the extension
- `file <path>`: reads a Netscape `cookies.txt` or a JSON cookie export (a path alone means `file`)
- `firefox`: reads the cookies from your Firefox
- `safari`: macOS only; needs Full Disk Access for the terminal
- `qr`, `phone`: Telegram only: a QR code to scan, or your number and the code Telegram sends

Only the cookies of the plugin's domains are kept, and the login is refused unless one of the
plugin's login cookies is there. A cookie plugin's login cookies are in [Plugins](#plugins). Off a
terminal (or with `--json`) leaving the method out is `<code>: how to log in?` with the hint `flnd
auth login <code> <method>, a method of <list>`.

- unknown plugin: `unknown plugin '<code>'`, hint `flnd plugin list shows the plugins`
- a method the plugin lacks: `<code> logs in by <methods>, not '<method>'`
- a plugin with no login (`mht`, `web`): `<code> needs no login`
- `file` without a readable path: `<code>: a file login needs the path of a cookie file`, hint `flnd
  auth login <code> file <path>`

Accounts: each account the site names is kept apart; logging in again as one replaces its login. On
a terminal the login asks for an optional "Account name" (the site's own name, else `account<n>`);
off a terminal the offered name is taken. The first account is the plugin's default. A target runs
as the account that last worked for it, else the first `ok` one.

- `flnd auth list`: every account, its plugin, name, default mark, origin, and status (`ok`, `held 4
  min`, `expires in 2 days`, `expired`). On a terminal `*` makes the account under the cursor the
  default, `a` logs one in, `d` logs it out.
- `flnd auth logout <plugin> [account]`: the account, or the default one. A terminal is asked first.
- **Switching.** `switch = true` (under `[defaults]`, or one `[plugins.<code>]`) moves a request its
  account cannot make to the plugin's next account, in `auth list` order: a block, a refused login
  (the account is marked `expired`), `Hidden`, a first page that is `NotFound`. An account that
  already ran the request, is expired or is held is skipped. A request never returns to an account
  it left; after every one it waits or fails as without switching. Each move is a `switch` record in
  the journal.
- **Walls.** A page that lands on a login wall the plugin declares waits 30 s, then 2 min, then
  counts as a refused login.
- **Expiry.** A run warns when a login cookie has expired or expires within 3 days, and writes back
  the cookies the site refreshed.

### The vault

Logins are sealed in `metadata/secret.db` (AES-256-GCM, bound to each row). The 32-byte key is
`~/.config/flnd/key` (`%APPDATA%\flnd\key`), mode 0600, one per user, made at the first login, and
never inside a home: a key file in a home is refused, so a copied home carries no key. With the key
gone, reading says `the vault's key is gone` (hint: log in again); rows are kept and open again when
the key returns, and the run marks the account `expired`. A cookie's value is never printed, logged
or put in an error: messages name the file, line, browser or cookie name.

### The Chrome extension

The extension lets flnd use the browser you are logged in with: read cookies, open tabs, read their
pages and answers, click and scroll. It works in Chrome, Chromium, Edge and Brave.

- `flnd extension install` copies it to `~/.config/flnd/extension/unpacked/`, writes the
  native-messaging host for every Chromium browser found, prints three steps (open
  `chrome://extensions`, turn on Developer mode, Load unpacked on that folder), and waits up to 10
  minutes for it to connect. `flnd extension uninstall` removes the folder and the hosts.
- `flnd host` is hidden: Chrome starts it with the extension's origin and it refuses any other. It
  relays between Chrome (native messages) and flnd's runs (a socket, a named pipe on Windows) under
  a secret kept outside the extension.
- The extension reads only the domains the built-in plugins own, which `make manifest` writes into
  its permissions; a site it may not read is a `UsageError` with a hint to allow it in the popup.
  The popup lists a tab's media and sends a target to `get` or `follow`.
- Ops: `hello cookies tabs.open tabs.list tabs.close capture.start capture.read capture.stop
  document click scroll evaluate storage screenshot media`. Not connected, a tab says `flnd's Chrome
  extension isn't connected`, hint `flnd extension install`.

## When it goes wrong

Exit codes:

- 0: all worked
- 1: some files or targets failed (named above the exit); `flnd status` exits 1 when something needs
  care
- 2: a usage error: unknown command or flag, a target no plugin reads, a category its route lacks, a
  bad `--when`, `--limit` or `--folder`
- 3: a login is needed, was refused, or `flnd auth login` failed
- 4: flnd itself stopped: a full or refused disk, a missing tool, a broken plugin, a bug
- 130: Ctrl-C

A mistake you can fix prints `✗ <message>` then `  hint  <what to do>`, never a traceback. A flag
mistake is `✗ flnd: unrecognized arguments: --nope` with the hint `flnd -h lists what it takes`. A
bug prints `✗ flnd hit a bug: <error>` and `hint  details in <home>/log/crash-<time>.txt` (exit 4;
`-v` prints the traceback instead). With `--json` every error is also one line, `{"type": "Error",
"error": {"class", "message", "hint"}}`.

Every error is a `flnd.Error` with `str(e)` (one line, no secret), `hint`, `target` and `retryable`.
Exit codes go by class, never by name.

```text
Error
├── UsageError     exit 2   a bad target, flag or setting; raised before any request
├── LoginError     exit 3   not logged in, expired, refused (401 or 403 on its domains)
├── SiteError               the site answered, but not usefully (.status, .response)
│   ├── Blocked             rate limited or blocked (.wait seconds, or None)
│   ├── NotFound            404 or 410: recorded gone
│   ├── Hidden              it exists, but this account cannot see it
│   └── Changed             the answer no longer has the expected shape
├── NetworkError            DNS, TLS, timeout, reset, a cut transfer
├── DiskError      exit 4   no space, no permission, a long name (.path); stops the run
│   └── BusyError           another run holds the part: that file is skipped
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
- a block a plugin reads from a page (`Blocked`): the account is held 30 s, then 2 min, then 8 min;
  ten quiet minutes restart the rounds; the fourth round fails the target
- 404, 410 (`NotFound`): never retried; the file is recorded gone, the target goes on
- not visible to this account (`Hidden`): the job moves to another account with `switch`, else fails
- 401, 403 on the plugin's domains (`LoginError`): not retried; `switch` moves on, else the account
  is marked expired
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
target's next runs (at most 3 later runs; after that press `r` on the target in `flnd target list`);
a target whose listing fails prints its error and hint and the other targets run; a usage mistake
stops the command before anything is fetched.

Messages you will meet:

- `<code>: not logged in, using public access; run flnd auth login <code>`: warning: only public
  posts
- `<code>: the site refused the login (HTTP 403)`: the login expired or was revoked: log in again
- `<home>/flnd.db: locked by another process, for 30s`: wait, or use another `--home`
- `unexpected <Type>: ...`: a bug: run with `-v` and report it
- `ins: not logged in, so there is no account to read`: `ins:me` needs a login

`classify` gives an error without a hint flnd's: a login error about a target gets `run flnd auth
login <code>`; a full or quota disk `free some space on that disk, or use another --home`; a
read-only one `that disk is read-only; use another --home`; a long name `use a shorter --home or
folder template`; any other exception from a plugin becomes a `PluginError`, `this is a bug; run
with -v and report it`. A URL in a message loses its user info, query and fragment; secret headers
become `***`; samples replace values under keys like `token`, `cookie`, `session`, `auth`,
`password`, `secret`, `key`, `sig` with `...` and never keep a login's answer.

### check and repair

`flnd status` shows the last run that downloaded (its command, times, saves, failures grouped by
target: 10 shown, then "and N more"), the accounts that are held, expired or expire within a week,
and the disk against the history. `check` compares the rows with the disk:

- **missing**: a row's file is gone. A repair removes the row; `flnd get <target> --again` then
  fetches it.
- **wrong size**: the repair takes size, time and hash from the disk.
- **unknown**: a file under `media/` with no row. Reported, never changed, so the exit stays 1.

Never unknown: parts, hidden files, symlinks, hard links of known files, sidecars, extracted archive
folders, and files renamed into place in the last minute. On a terminal `flnd status` asks `Repair 2
rows? (y/N)`; a yes is `fl.check(fix=True)`. It never deletes, moves or downloads a file. Failure
kinds: `network blocked site changed login disk tool bug usage gone error`. A home of an older
version migrates on first open, after copying `metadata/flnd.v1.db`.

## Commands and flags

`flnd --help` lists every command and the plugins' codes; `flnd get --help` adds a row per plugin
with its targets and categories. Help imports no plugin. Global flags go before or after the
command.

| Global flag | Default | What it does |
| --- | --- | --- |
| `--home DIR` | `$FLND_HOME`, else `./.flnd` | the home |
| `-v`, `--verbose` | off | show debug lines and a bug's traceback |
| `--json` | off | print events and rows as JSON lines (each has a `type`); never asks |
| `-h`, `--help` | | print help for flnd or the command |
| `-V`, `--version` | | print `flnd <version>` |

**`flnd get [target...]`**: get what the targets have, and follow them. With no target it reads
standard input, else gets every followed target, or prints `nothing to sync` and exits 0.

| Flag | Default | What it does |
| --- | --- | --- |
| `target...` | stdin, then followed | see [Targets](#targets) |
| `--when WHEN` | `[defaults] when`, else every day | see [Dates](#dates) |
| `--limit N` | none | at most N posts per target, or N targets per search; N at least 1 |
| `--dry` | off | list what would be saved; write nothing |
| `--folder VALUE` | each target's folder | a template or a list ([Folders](#folders)) |
| `--again` | off | list every post again; ask again for gone files |
| `--exec CMD` | none | run CMD for each saved file, without a shell |

**`flnd target list`** the targets the home knows: files, size, last run, categories. On a terminal:
arrows move, `/` filters, `enter` opens details, `f` follows or unfollows, `r` makes failed files
retryable, `a` asks for a target and gets it, `d` forgets one after a confirmation, `q` quits. On a
pipe a plain table; `--json` one `Tracked` object each.

**`flnd target clean <target...> [--media]`** forgets the targets (rows, files rows, failures,
cursors, heads, gaps). An unknown target is a usage error. `--media` (default off) also deletes
their recorded files, sidecars, links, extracted folders and the folders left empty; files no row
names are never deleted. A terminal is asked first.

**`flnd auth list | login <plugin> [method] [path] | logout <plugin> [account]`**: see [Logins and
accounts](#logins-and-accounts).

**`flnd plugin list`, `flnd plugin install <path>`, `flnd plugin uninstall <code>`**: `list` shows
built-in, installed and disabled plugins (kind, API, domains, targets using it, a disabled one's
reason). `install` checks a `.py` file or a package folder with `__init__.py`, copies it into the
plugin folder and records it. It refuses a home or cookie file, a link, a special file, a file with
other hard links, a built-in's code, and an API this flnd lacks. `uninstall` removes an installed
plugin (`<code> is built in; it can't be uninstalled`, `no plugin named <code> is installed`); its
accounts and targets stay. Plugins load from `$FLND_PLUGINS`, else `~/.config/flnd/plugins/`, never
from a home.

**`flnd extension install | uninstall`**: see [The Chrome extension](#the-chrome-extension).

**`flnd status`**: see [check and repair](#check-and-repair). `--json` prints one `{"type":
"Status", "status", "accounts", "disk"}`.

**`flnd config`** lists each key, value, default and origin (`home`, `user`, `default`). On a
terminal `e` edits, `a` adds a key, `d` resets one; comments in the file stay. `--json` prints one
`Setting` per key.

**`flnd host [origin] [--parent-window ...]`**: hidden; Chrome starts it for the extension.

What a run prints: on a terminal a live screen on standard error (a row per target, files in flight,
the `now` line); on a pipe only the end summary. The end summary goes to standard output (`done in
4m 12s · 3 targets · 1,021 saved (3.3 GB) · 1,220 already saved · 1 failed`, starting with `✓` when
nothing failed); up to 10 failures follow on standard error, each with its message and hint, then
`details  flnd status`. Warnings repeated within 10 s fold into one line, `text (x2)`. Text from a
site is stripped of control codes before it is printed. Each run's events go to
`log/<time>_get.jsonl`.

## Config

`config.toml` in the home, and the user file (`~/.config/flnd/config.toml`,
`%APPDATA%\flnd\config.toml`), hold optional settings. A bad value stops the command with a usage
error naming the file, the key and what it takes, never the value: `config.toml: plugins.ins.pace is
a number of seconds, 0 or more, not this text`. An unknown key or table is a warning, said once, and
ignored (`[sources.<code>]` reads as `[plugins.<code>]`). `flnd config` edits it.

`[defaults]`:

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `dedupe` | bool | `true` | hard-link a file whose bytes are saved already |
| `sidecars` | bool | `false` | write `<file>.json` beside each file |
| `when` | text | `""` | days a get takes, as `--when` writes them; also bounds a sync |
| `exec` | list | none | program and arguments run per saved file; the **user file only** |
| `exec_timeout` | int | `3600` | seconds an `exec` program may run before it is killed; 0 none |
| `rate` | text | `"0"` | the run's speed limit (`"20 MB/s"`, `"500 KB/s"`; `"0"` off) |
| `slowest` | text | `"0"` | cut a transfer under this speed over 30 s and go on |
| `height` | int | `0` | tallest video wanted in pixels; checked, not yet read by the engine |
| `switch` | bool | `false` | move a refused request to the plugin's next account |

`[plugins.<code>]` (one table per plugin):

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `pace` | number | the plugin's | seconds between page requests of one account; 0 or more |
| `limits` | table | the plugin's | `"<host>" = n` files at once (n at least 1), merged over its |
| `proxy` | text | none | `http://`, `https://`, `socks5://` or `socks5h://` with host and port |
| `stop_after` | int | `5` | saved posts in a row that end a newest-first sync; at least 1 |
| `browser` | text | none | `"chromium"` or `"chrome"`, for an `ANY` browser plugin; unread |
| `switch` | bool | `[defaults]`'s | account switching for this plugin |
| `folder` | text | the plugin's | folder template ([Folders](#folders)) |
| `filename` | text | the plugin's | file name template |

A plugin's own settings (its `Settings` struct) are keys of its table and reach it as
`ctx.settings`. Notes:

- `rate` is one byte bucket for the run, a second deep. Before a file of known size starts its disk
  needs room for it, the bytes due from files in flight, and 64 MiB; else `DiskError`.
- `proxy` carries every request and download of the plugin's HTTP session, not the browser, so
  plugins that open pages (ins, tik, fbk, twt) load them directly and warn once. `socks5h://` looks
  names up through the proxy; the URL may hold a user and password.
- `pace` and `limits` win over the plugin's class. A host's cap still adapts under the file's
  number.
- `exec` in a home's file is ignored with a warning; given in a call it is a usage error:
  `defaults.exec is read only from the user file, not from a call`. `fl.get(exec=[...])` and
  `--exec` hand a program to one run, over the file's.
- Gone keys: `[defaults] again`, `[vault]`, `scroll` (unknown now); `dedupe = "link"` reads as
  `true`, `"off"` as `false`, each with a warning.

```toml
[defaults]
when = "1y"
switch = true

[plugins.ins]
pace = 3.0
limits = { "cdninstagram.com" = 8 }
proxy = "socks5://127.0.0.1:1080"
```

## Plugins

One section per built-in plugin, API 2, in the order links are tried (`web` last). Targets and
refusals are in [Targets](#targets). Each section lists categories (`own` ones are the default,
`opt-in` ones need a name or `all`; NF marks newest-first, the only ones that stop early), folder,
file name and history key templates, login, limits and walls, and known gaps. Everything here is the
code's own declaration; `folder` is under `media/<code>/<value>/`.

Shared by every cookie login: methods `browser chrome file firefox safari`; the cookies of the
plugin's domains are kept; a run warns when the login cookie expires within 3 days. Walls and blocks
are held 30 s, 2 min, 8 min by the engine; the fourth block in a row fails the target. Pace is the
least seconds between two page requests of one account; each CDN host's files go 8 at once, adapting
up to 32.

### ins (Instagram)

- **Reads:** a profile's posts, reels, tagged, reposts, stories, highlights and profile picture; one
  post or reel by link; `ins:me`. Logged in, `posts`, single posts, lazy posts and `ins:me` use the
  web API from inside the tab, else pages are read.
- **Categories:** `posts` NF, `reels` NF, `stories`, `highlights`, `profile` (own); `tagged`,
  `reposts` NF (opt-in). A pinned post does not count toward the stop for older posts.
- **Folder:** `{category}`; `highlights/{album}` (a folder per highlight); `profile`.
- **Filename:** `{date}_{post}{index}`; a story with music also saves `..._music.m4a`.
- **Key:** `{post}_{num}` (`ins:C1abc_0`); `stories`, `highlights`, `profile` key by `{path}`.
- **Login:** cookie `sessionid` (optional; `ins:me` needs it, and reads the account id from
  `ds_user_id` or the start of `sessionid`).
- **Limits and walls:** pace 2.0; no host limits; walls `/accounts/login`, `/challenge`,
  `/accounts/suspended`; "Please wait a few minutes" answers are blocks.
- **Known gaps:** story links are refused; only stories active now; a private post is `NotFound`,
  not `Hidden`; `tagged`, `stories`, `highlights`, `profile` are read in full on every run.
- **Saved as:** `media/ins/example_user/posts/2026-01-02_Cexample123_0.jpg`,
  `media/ins/example_user/highlights/Travel/2026-01-02_1234567890.mp4`

### thr (Threads)

- **Reads:** a profile's posts, replies, reposts and picture; one post by link: the page's embedded
  data, then its GraphQL feed scrolled to its end. No `me`, no search. Unconfirmed live.
- **Categories:** `posts` NF, `replies` NF, `profile` (own); `reposts` NF (opt-in). Only a post of
  the profile's own is kept in `posts` and `replies`; a post without files is skipped.
- **Folder:** `{category}`; `profile`. A single post goes in its owner's folder, as a reply (its
  `reply_to_author` set) under `replies`, else `posts`.
- **Filename:** `{date}_{post}{index}`; a carousel or video post saves each file, a video as its
  best MP4.
- **Key:** `{post}_{num}` (`thr:C1abc_0`); `profile` keys by `{path}`.
- **Login:** cookie `sessionid`, as Instagram's (optional).
- **Limits and walls:** pace 2.0; wall `/login`; "Please wait a few minutes" answers are blocks;
  429s hold and ask the same page again.
- **Known gaps:** a post is opened by `/t/<code>`; only images and videos are saved.
- **Saved as:** `media/thr/example_user/posts/2026-01-02_C1abc_0.jpg`

### twt (Twitter/X)

- **Reads:** a user's posts, Media tab, highlights, reposts, replies, avatar and banner; one tweet;
  a search; the accounts you follow. Profile and tweets: GraphQL; timelines and searches: scrolled.
- **Categories:** `posts` NF, `media` NF, `highlights`, `replies` NF, `profile` (own); `reposts`
  (opt-in). Retweets are left out of every category but `reposts`; a tweet's files include the media
  of a tweet it quotes and its card image.
- **Folder:** `{part}` (the category; `media` uses `images` and `videos`); `profile`.
- **Filename:** `{date}_{post}_{n}` (`n` is `0`, `1`, ..., `q0` for a quoted tweet, `card`).
- **Key:** `{post}_{n}` (`twt:<tweet id>_<n>`); `profile` keys by `{path}`.
- **Search:** `twt?<words>` opens the Latest tab; X operators (`from:`, `filter:media`) work;
  `--when` is sent as `since:`/`until:`. When X's cursor ends early (or a 429 cuts a window), a
  `max_id:` window follows from the oldest of the window's last 5 tweets, so an old outlier
  skips nothing; it ends when a window adds no new tweet; 3 empty pages in a row are passed; a 429 holds that bucket. Each tweet with files
  is a `post` target in the search's `--folder`. Tagged: `twt?@example_handle -from:example_handle`.
- **Login:** cookie `auth_token` (a logged-out browser lands on the login wall); API calls send
  `ct0` back as the CSRF token. `twt:me`: `Following` for the `twid` cookie's id, paged by `Bottom`;
  no login is `LoginError`, hint `log in: flnd auth login twt`. Query id and path: unconfirmed live.
- **Limits and walls:** pace 0.2 per bucket (a 429 holds only its operation, and a timeline
  goes on with `resume`); walls `/i/flow/login`, `/login`, `/account/access`. Query ids are
  refreshed from x.com's script on a 400 or 404, at most once a run.
- **Known gaps:** a single tweet has no browser fallback; a protected and a deleted tweet are both
  `NotFound`; video is MP4 only (streaming-only is skipped); text-only tweets save nothing.
- **Saved as:** `media/twt/example_handle/posts/2026-01-02_1234567890_0.jpg`

### tik (TikTok)

- **Reads:** a profile in the browser: the first scroll captures the page's own
  `api/post/item_list/` request, whose pages go by `cursor` in the page (confirm live), else it
  scrolls (logged). One post from the embed page; a search, short link and `tik:me` in the browser
  (see Targets).
- **Categories:** `videos` NF, `stories`, `playlists` NF, `profile` (own); `reposts`, `liked`,
  `favorites` (opt-in; the last two only if public). `videos` and `playlists` share one scroll.
- **Folder:** `{part}/{album}`: `part` is `videos` or `images` for a post, `albums` for a playlist
  video (`album` is the playlist's name), else the category; `profile` for `profile`.
- **Filename:** `{date}_{post}_{idx}` (`idx` empty for a video; a photo's place from 1).
- **Key:** `{post}_{idx}` (`tik:<id>`, `tik:<id>_<n>`); `profile` keys `profile/avatar_<id>.jpg`.
- **Login:** cookies `sessionid` or `sid_tt` (optional).
- **Limits and walls:** pace 0.2; no walls. A captcha gets 60 s in the browser window (a hidden one
  is shown), then a block hold; a fourth unsolved one fails the job; a post list stopped short by
  one is read again (3 times at most). A 429 holds and carries on from the same cursor; an empty
  page that says `hasMore` holds once, then ends with a warning. A walled post list fails `videos`
  and `playlists` with `Hidden`.
- **Known gaps:** no such tab is logged; a tab deaf to the click for 10 s fails `Changed`; stories
  only as the page shows them on load.
- **Saved as:** `media/tik/example_creator/videos/2026-01-02_1234567890123456789.mp4`,
  `media/tik/example_creator/albums/Example_Playlist/2026-01-03_1234567890123456781.mp4`

### fbk (Facebook)

- **Reads:** a profile or page in the browser, and one video, reel, photo or post by link (a
  `fb.watch` or `/share/` link is opened to find where it lands). A tab's first scroll captures its
  GraphQL POST; its pages then go by the same body with the cursor (`cursor`, `after` or
  `afterCursor`) replaced, in the page. With no capture or an unreadable answer it scrolls.
- **Categories:** every tab is read to its end each run. `posts`, `photos`, `albums`, `reels`,
  `videos`, `stories`, `profile` (own); `tagged` (opt-in). A video found in another tab goes to
  `videos`, a photo naming its album to `albums/<album>`; others' media is left out.
- **Folder:** `{part}/{album}`; `profile` for `profile`. **Filename:** `{date}_{post}`; profile
  pictures `profile_<id>`, covers `cover_<id>`.
- **Key:** `{post}` (`fbk:<id>`, so a photo in two tabs is saved once); `profile` keys `{path}`.
- **Login:** cookies `c_user` or `xs` (optional, but a logged-out browser sees little).
- **Limits and walls:** pace 0.2; walls `/login`, `/login.php`. A 429, a "temporarily blocked" page,
  Comet's error line and GraphQL `errors` codes 1675004 and 368 (confirm live) hold, then carry on
  from the same cursor. The CDN needs `Referer: https://www.facebook.com/`.
- **Known gaps:** `/photos /groups /events /stories /marketplace /hashtag` are refused; a page is
  told from a profile by the header's `__typename` (confirm live); stories are those active now;
  without a header `stories` and `profile` are skipped; undated photos take a neighbour's date.
- **Saved as:** `media/fbk/example.page/photos/2026-01-02_1234567890.jpg`,
  `media/fbk/example.page/albums/Holiday/2026-01-02_1234567891.jpg`

### tlg (Telegram)

- **Reads:** the photos, videos and other files of a chat, group or channel your account can see,
  through Telethon (MTProto), never a web page. Documents of 16 MiB or more are fetched in up to 4
  parts; a part keeps its offsets in `.part.json` and resumes.
- **Categories:** `photo`, `video`, `file`, all NF and own, each read with a server-side filter.
- **Folder:** `{chat}/{category}`; `chat` is the title, a person's name, else username or id.
- **Filename:** the plugin's: `<date>_<message id>` then `_<original name>` or the extension.
- **Key:** `{chat_id}:{post}` (`tlg:<chat id>:<message id>`).
- **Login:** a Telegram session, required. Methods `browser` (reads Telegram Web's login from
  Chrome's `Default` profile), `qr`, `phone`; asks once for `api_id` and `api_hash` from
  `my.telegram.org`. Without one: `tlg: not logged in`; a revoked session: `tlg: the saved login no
  longer works`.
- **Limits and walls:** `limits = {"t.me": 3}`; pace does not apply. A flood wait up to 60 s is
  slept; a longer one is a block hold (30 s, 2 min, 8 min) and listing resumes after it.
- **Known gaps:** an invite you have not joined is `NotFound` (`join it first`), flnd never joins; a
  private or left chat is `Hidden`; a message link downloads the whole chat; two chats with one
  title fail with both ids.
- **Saved as:** `media/tlg/examplechannel/Example_Channel/photo/2026-01-02_1234.jpg`

### loi (LOIBUS)

- **Reads:** one post: `info.md` (its fields, description, tags, links), previews and, for a VIP
  account, archives. HTTP to `api.moegoat.com`, signed as the site's script signs; links are AES.
- **Categories:** `archive` NF (info.md with links, the archives) then `photo` NF (previews; info.md
  without links only when `archive` is not wanted), both own.
- **Folder:** `{author}/{post} {title}/{part}`, from `media/loi/` (`author` is the post's category,
  `misc` when none; `part` is `images` for previews). **Filename:** `01.jpg`, `02.jpg`, archives
  keep their URL's name. **Key:** `{path}` (`loi:<path>`).
- **Login:** cookie `access-token` (optional; a Bearer token). Archives need a VIP account; without
  one `archive` warns and writes info.md without links.
- **Limits and walls:** pace 0.5. Daily archive quota by VIP level: 15, 25, 40, 60 (SVIP doubles),
  read once per run, counted per UTC day in the plugin's state; once it is used up `archive` skips
  the day's posts with a warning, and the run says how many were left for tomorrow. A 429 holds for
  its `Retry-After`; a JSON rate answer is a block hold.
- **Search:** `loi?<words>` reads `GET api/lois?keyword=<words>&page=<n>&page_size=20` (confirm
  live: endpoint, parameters, answer shape): each page's `data.list` ids as `loi:post/<id>`, ended
  by a short page; rate answers are held like the links'.
- **Known gaps:** no author or tag target; a post counts once its links are fetched; Baidu links
  go to info.md only; an expired token reads as "no download links".
- **Saved as:** `media/loi/example_author/1234567890_Example_Title/info.md`

### mht (mimihentai)

- **Reads:** the site's public JSON API over HTTP; no login, no browser. A manga's chapters (pages
  numbered `001`, `002`, ...) and its cover; `author`, `genre` and `query` find manga and each is
  got as its own `mht:<id>` target. Authors and genres match ignoring case and accents.
- **Categories:** `chapters`, `cover` (own). Not newest first: every chapter is listed, and the
  engine skips saved ones. A chapter is single; the three finders pass categories on.
- **Folder:** `chapters/{order} {title}` (`order` zero-padded to 3 digits when whole); `cover` for
  `cover`. **Filename:** the plugin's (`001`, `cover`; the engine adds the extension). **Key:**
  `{post}_{num}` (`mht:<chapter id>_<n>`); `cover` keys `{post}_cover`.
- **Login:** none: `flnd auth login mht` is `mht needs no login`.
- **Limits and walls:** pace 0.5; no walls. Searches page by cursor and stop on a missing, empty or
  repeated cursor.
- **Known gaps:** `chapters:<n>` is an exact count, checked on each result; negation works for
  genres only; `mht:<number>` is always a manga.
- **Saved as:** `media/mht/1234567890/chapters/001_Example_Chapter/001.jpg`

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

## Python

A script uses flnd through one object: `flnd.open(home)` gives a `Flnd`, and its methods and groups
are the CLI's commands. Every `python` block on this page runs offline in the docs test, in one
shared namespace, page order. Nothing is written until a call saves something. `import flnd` loads
only the package's entry file; each name loads its module on first use.

Every call, and the CLI command it matches:

| Call | Returns | Same as |
| --- | --- | --- |
| `flnd.open(home=None, *, again=None)` | `Flnd`, a context manager | `--home`, `--again` |
| `fl.get(*targets, when=, limit=, dry=, folder=, exec=)` | `Run`, not started | `flnd get` |
| `fl.targets` / `.list(plugin=None)` | `list[Tracked]` | `flnd target list` |
| `fl.targets.follow(*t)`, `.unfollow(*t)`, `.retry(*t)` | `list[Tracked]` | `f`, `r` in the list |
| `fl.targets.clean(*t, media=False)` | `list[Tracked]` | `flnd target clean` |
| `fl.files(target=None, kind=None, since=None)` | `list[Record]` | reads `flnd.db` |
| `fl.auth` | accounts, `Account` each | `flnd auth list` |
| `fl.auth.login(plugin, *, method=None, path=None, name=None)` | `Account` | `flnd auth login` |
| `fl.auth.logout(plugin, name=None)`, `.default(plugin, name)` | `Account` | `flnd auth logout` |
| `fl.plugins`, `.install(path)`, `.uninstall(code)` | `Installed` | `flnd plugin ...` |
| `fl.extension.install()`, `.uninstall()`, `.connected`, `.version` | | `flnd extension ...` |
| `fl.add(PluginClass)` | | a plugin for this `Flnd` alone |
| `async with fl.plugin(code, account=None) as ctx` | the plugin's `Ctx` | a script's own loop |
| `fl.status` | `Status` or None | `flnd status` |
| `fl.check(*, fix=False)` | `Checked` | `flnd status` repair |
| `fl.config` (dotted names), `.where(k)`, `.default(k)`, `.set(k, v)`, `.unset(k)` | | `config` |
| `fl.close()` | | leaving the `with` stops every run |

Options are keyword-only. Left as None, `when` takes `[defaults] when`. A mistake raises
`flnd.UsageError` at the call, before the run exists.

### Setup: a scratch home and an offline plugin

```python
import datetime
import pathlib
import tempfile

import flnd
from flnd.plugin import File, Plugin, Post, route

home = tempfile.mkdtemp()

def text_file(name, body):
    data = body.encode()

    async def fetch(part):
        part.total(len(data))
        await part.write(data[part.offset:part.end])

    return File(fetch=fetch, name=name)

class Demo(Plugin):
    code, name, domains, api = "dmo", "Demo", ("demo.example.com",), 2

    @route("dmo:{user}")
    async def user(self, ctx, user):
        return user

    @user.category
    async def posts(self, ctx, user):
        yield Post("p2", [text_file("p2.txt", "newer")], date=datetime.date(2026, 3, 2))
        yield Post("p1", [text_file("p1.txt", "older")], date=datetime.date(2026, 2, 1))

    @user.category(own=False, newest_first=False)
    async def tagged(self, ctx, user):
        yield Post("t1", [text_file("t1.txt", "tagged")], date=datetime.date(2026, 3, 5))
```

### Dry run with a date window and categories

`Planned` events say what a run would save; `all` takes the opt-in `tagged` too.

```python
with flnd.open(home) as fl:
    fl.add(Demo)
    preview = list(fl.get("dmo:example_user|all", when="2026-03", dry=True))
    assert [(e.post, e.state) for e in preview] == [("p2", "new"), ("t1", "new")]
```

### Stream events and read the report

Iterating a `Run` starts it and yields each event: `Saved` (target, category, post, path, date,
size, retried), `Skipped` (reason `saved`, `nothing new` or `busy`), `Failed` (target, error, and
for a file post and path), `Planned` (state `new` or `saved`). `run.wait()` returns the `Report`
(`saved skipped bytes planned failed stopped retried resumed unchanged`, and `ok`). `run.state` is a
live snapshot (`State`, `TargetState`, `FileState`, `HoldState`, `Phase`); `run.stop()` stops as a
first Ctrl-C does; `async for` and `await run` work from asyncio.

```python
with flnd.open(home) as fl:
    fl.add(Demo)
    saved = []
    run = fl.get("dmo:example_user")
    for event in run:
        match event:
            case flnd.Saved(path=path, post=post):
                saved.append((post, path.name))
            case flnd.Failed(error=error):
                raise RuntimeError(f"{error} ({error.hint})")
    assert sorted(saved) == [("p1", "p1.txt"), ("p2", "p2.txt")]
    assert run.report.ok and run.report.saved == 2
```

### Sync and `again=True`

```python
with flnd.open(home) as fl:
    fl.add(Demo)
    assert fl.get().wait().saved == 0          # a sync of what is followed: nothing new

with flnd.open(home, again=True) as fl:
    fl.add(Demo)
    assert fl.get("dmo:example_user").wait().skipped == 2     # lists every post again
```

### Check, repair, re-fetch, reset retries

```python
with flnd.open(home) as fl:
    fl.add(Demo)
    first = fl.files(target="dmo:example_user")[0]
    first.path.unlink()
    assert not fl.check().ok and len(fl.check().missing) == 1
    assert fl.check(fix=True).fixed == 1
    fl.targets.retry("dmo:example_user")

with flnd.open(home, again=True) as fl:
    fl.add(Demo)
    assert fl.get("dmo:example_user").wait().saved == 1
```

### Folder and mistakes

```python
other = tempfile.mkdtemp()
with flnd.open(other) as fl:
    fl.add(Demo)
    events = list(fl.get("dmo:example_user|all", folder="{target}/{date}"))
    paths = sorted(e.path.relative_to(pathlib.Path(other, "media", "dmo")).as_posix()
                   for e in events if isinstance(e, flnd.Saved))
    assert paths == ["example_user/2026-02-01/p1.txt", "example_user/2026-03-02/p2.txt",
                     "example_user/2026-03-05/t1.txt"]
    for call, said in [
        (lambda: fl.get("dmo:example_user|stories"), "dmo has no category stories"),
        (lambda: fl.get("dmo:example_user", limit=0), "limit must be at least 1"),
        (lambda: fl.get("dmo:example_user", folder="../x"),
         "the call: folder is a template inside the target's folder, not this text"),
    ]:
        try:
            call()
        except flnd.UsageError as error:
            assert str(error) == said
```

### Accounts and settings

```python
def write_cookies(filename):
    path = pathlib.Path(home, filename)
    path.write_text("# Netscape HTTP Cookie File\n"
                    ".instagram.com\tTRUE\t/\tTRUE\t2000000000\tsessionid\texample-value\n")
    return str(path)

with flnd.open(home) as fl:
    a1 = fl.auth.login("ins", path=write_cookies("c1.txt"), name="example_user")
    a2 = fl.auth.login("ins", path=write_cookies("c2.txt"), name="example_creator")
    assert a1.is_default and not a2.is_default
    fl.auth.default("ins", "example_creator")
    assert [a.name for a in fl.auth if a.plugin == "ins" and a.is_default] == ["example_creator"]
    fl.config.set("plugins.ins.switch", True)
    assert fl.config["plugins.ins.switch"] is True
```

`fl.auth.login("ins", method="chrome")` reads a browser's cookies the same way. An error that stops
a whole run (a full disk) is raised when the run is read; one file's or one target's failure is a
`Failed` event and the run goes on. Events hold data, never live objects, and the one exception is
`Failed.error`, the `flnd.Error`.

## Writing a plugin

A plugin is one class for one site: which targets it reads, and the posts each has. flnd does the
pacing, retries, downloading, naming, dating, skipping, early stops, resuming, progress and logins.
Plugin API 2 is `flnd.plugin`; `flnd.testing` tests it; `tests/example_plugin.py` is a bigger
example and `src/plugins/` holds real ones.

The contract:

- `code` (text): 2 to 16 lowercase letters or digits, a letter first: `exm:<user>`
- `name`, `api` (text, int): the site's name; `api = 2` is required
- `domains` (tuple): the plugin's hosts and subdomains; requests to them are paced, signed, with
  cookies
- `login` (`cookies(...)` or a `Login`): `cookies("token", url=..., required=False)`; none means no
  login
- `pace` (float): least seconds between page requests of a bucket (0.5)
- `limits` (dict): files at once per host; caps adapt up to 32
- `folder`, `filename`, `key` (templates): where, what name, and the history key
  ([Folders](#folders))
- `fields` (tuple): post fields the templates may use
- `unsupported` (dict): `{pattern: reason}`: a link refused with the reason
- `walls` (tuple): login-wall paths, matched as whole segments
- `assets` (bool): its tabs load images, media and fonts; False (default) aborts those requests, so
  the responses a plugin reads are unchanged. Live-checked (2026-11-20): `fbk`, `ins`, `twt` block
- `hidden` (bool): its tabs may be in a hidden window (False). The browser is hidden only while
  every plugin using it is; a handoff (a captcha) closes it and reopens it visible, same cookies
  and tabs. A `browser` login is always visible. Hidden after a live check: `fbk`, `ins`, `twt`. A
  hidden tab reads as a visible one: same user agent and client hints, a desktop screen and window,
  no software-GL name, and agreeing notification state (`tests/test_hidden.py`)
- `@route(patterns, single=, discovers=, shown=, folder=, newest_first=)` (method): reads one kind
  of target; a `single` route's `Post(owner=, category=)` file it in the owner's folder ([A single
  post](#a-single-post))
- `@route.category(own=, url=, newest_first=, folder=, key=)` (method): one part of a route's target
- `ctx.fetch`, `ctx.pages(req, cursor=, send=, resume=)`, `ctx.feed(..., resume=, bucket=)`,
  `ctx.open`, `ctx.pause`, `ctx.hold` (): the run's requests. `resume=True` holds a 429 through
  `ctx.hold` and goes on from the last page or cursor; past the last round: `Blocked`. A feed
  listens from its call; a scrolling one ends after 60 s with no new answer, warning `<target>: the
  list got no new answer in 1m; it ends here`. A page read gets 30 s (else `NetworkError`), a tab
  300 s for another's browser launch
- `sign(request, cookies)` (`def` hook): the request, signed, before each try
- `connect(ctx)` (async context manager): once per run; `date(ctx, post)` (`async def` hook): a day
- `blocked(response)` (`def` hook): seconds to hold, or None
- `refresh(ctx, post)` (`async def` hook): a link expired: the post, fresh
- `resolve(text)` (classmethod hook): text no pattern can say: a `Target` or None

A pattern is `exm:{user}` (after the code), `exm?{words}` (a search), `example.com/p/{id}` (a link)
or `example.com/watch?v={id}`. `{name}` matches a user name (a leading `@` dropped), `{id:\d+}`
gives a regex, `*` matches one path segment. A `[pattern, template]` pair builds the value from
several placeholders. Routes are tried in declaration order; the first match wins, so a narrow route
comes before a catch-all. A mistake in the class raises `PluginError` when it is made (a missing
`code`, a pattern outside the domains, a category declared twice, a hook of the wrong kind). The API
grows by additions; a break is API 3 with an adapter.

```python
from flnd.plugin import File, Plugin, Post, Request, cookies, route
from flnd.testing import FakeCtx, Reply, check, collect

API = "https://api.example.com"

class Example(Plugin):
    """example.com: a user's photos, from its JSON API."""

    code, name, domains, api = "exm", "Example", ("example.com",), 2
    login = cookies("token", url="https://www.example.com/login")
    filename = "{date}_{post}{index}"

    @route("exm:{user}", "example.com/{user}")
    async def user(self, ctx, user):
        """A profile: `exm:example_user`, or `https://www.example.com/example_user/`."""
        return (await ctx.fetch(f"{API}/users/{user}")).json

    @user.category
    async def posts(self, ctx, profile):
        """The profile's posts, newest first, page by page."""
        async for page in ctx.pages(f"{API}/users/{profile['id']}/posts", cursor="next"):
            for item in page.json["items"]:
                yield Post(item["id"], [File(url) for url in item["urls"]], date=item["time"])

    def sign(self, request, cookies):
        if not cookies.get("token"):
            return request
        return request.replace(headers={**(request.headers or {}),
                                        "Authorization": f"Bearer {cookies['token']}"})
```

### The test kit

`collect(plugin, target, ctx=, categories=, limit=)` runs a plugin on a `FakeCtx` that answers each
request from `routes` (a URL to a `Reply`, or a function of the `Request`), runs the route and its
categories as the engine would, and lists each file with its path, key and day. Pace, holds, block
rounds, wall waits and retries are the engine's, on a fake clock; `ctx.seen.requests` and
`ctx.seen.holds` record what happened. `check(Plugin)` re-checks the class and resolves every
example target in its routes' docstrings.

```python
ctx = FakeCtx(Example, cookies={"token": "example-token"}, routes={
    f"{API}/users/example_user": Reply({"id": "42"}),
    f"{API}/users/42/posts": Reply({"items": [
        {"id": "p1", "time": "2026-01-02",
         "urls": ["https://cdn.example.com/p1.jpg", "https://cdn.example.com/p1.mp4"]}]}),
})
files = collect(Example, "exm:example_user", ctx=ctx)
assert [f.path for f in files] == ["posts/2026-01-02_p1_0.jpg", "posts/2026-01-02_p1_1.mp4"]
assert [f.key for f in files] == ["p1_0", "p1_1"]
assert ctx.seen.requests[0][2]["headers"] == {"Authorization": "Bearer example-token"}
assert [str(found) for found in check(Example)] == ["exm:example_user", "exm:example_user"]
```

To ship it: `flnd plugin install <file or folder>` (an installed plugin loads from the plugin
folder, never from a home, and runs in flnd's process with your logins, so install only code you
trust), or `fl.add(Example)` for one script. A built-in is `src/plugins/<code>.py` importing only
`flnd.plugin` and the standard library (an SDK inside a function), with tests on `FakeCtx`, `make
manifest` run after any change to domains, routes or categories, and a section in this page.
`flnd.plugin.API` is 2; a plugin of another API is refused (`exm needs plugin API 3; this flnd has
2`, hint `update flnd`; `exm was written for plugin API 1; this flnd has 2`). Dropped names are
refused with their new spelling: `needs`, `requires_login`, `login_methods`, `logged_in`,
`concurrency`, `prepare`, `scroll`, `single_kinds` (see `OLD_NAMES` in `src/plugin.py`).

## For developers

Rules for changing the code are in `AGENTS.md`. The shape:

```text
flnkit/   standalone toolkit, imports nothing of flnd: net browser files html dig text clock
          db cookies vault media stream errors
src/      the package flnd
  _library.py cli.py errors.py plugin.py testing.py
  engine/  run jobs crawl save resolve plugins auth extension store config
  ui/      theme out screen lists forms (imports only flnkit, ui, events, errors)
  plugins/ the built-ins, importing only flnd.plugin and the standard library
  extension/ the Chrome extension
```

`tests/test_layers.py` checks the import rules. A run has one asyncio loop on its own thread; plugin
code is async; only `engine/jobs.py` retries, sleeps and picks threads. A commit is one per post and
never blocks the loop.

**The home's files.** `flnd.db` is SQLite in WAL mode, schema version 2: `files` (a row per file,
key `<code>:<key>`, with target, post, category, path, size, date, hash, state `saved`, `gone` or
`link`), `posts` (a view of `files`), `targets` (follow, categories, cursors, heads, gaps, `seen`,
`account`), `runs`, `failures` (class, message, hint, attempts, redacted URL), `state` (a plugin's
own values) and `accounts` (never a secret). One transaction per post; a file's row is written after
the file, so a crash leaves at worst a file without a row. `secret.db` holds the vault. A file
listed in two categories of a target is saved once, and the second is a link row `<key>@<category>`.
Read it with any SQLite tool.

**make targets**, run inside `flnd/`:

| Target | Does |
| --- | --- |
| `make check` | ruff (`make lint` alone), then every test; green at every commit |
| `make test ARGS="-k fetch"` | the tests, or some (`FLND_TEST_NO_PROFILES=1`) |
| `make api`, `make manifest` | regenerate `tests/data/api.txt` (read its diff); the manifest |
| `make bench` | offline benchmarks; `ARGS=--gate` fails on a 15% slowdown, `--record` rebases |
| `make docs`, `make dist` | the API reference into `build/docs/`; a one-file binary |
| `make try ARGS="ins:example_user"` | list, live, what a get would save (scratch home `.try`) |
| `make ext`, `make live`, `make cdn-real` | hand checks, never part of `check` |
| `make setup`, `make clean` | sync `.venv` from `uv.lock`; remove `build`, `dist`, `.try` |

**Benchmarks** (`tests/bench/`, groups `startup ui net disk db walk cpu e2e browser`) run offline
against a loopback server and scratch folders; `make check` runs each in smoke mode only. Run them
before a speed change for the "before" numbers, and `--gate` before its commit. The `browser`
group also reports `heavy_*` and `lean_*` (peak MiB, CPU seconds of Chrome's processes on a scrolled
page of 300 images, loaded or blocked), read with `ps`, not on Windows.
