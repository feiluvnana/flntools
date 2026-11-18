# Storage: the home folder

Everything flnd keeps lives in one folder, the **home**: `$FLND_HOME`, else `.flnd` in the folder
flnd runs from; the library takes `flnd.open(home)`. This page lists every file in it and what
each holds, so you can back it up, move it, or read it with your own tools.

Never share a home with someone you wouldn't give your logins to: `metadata/secret.db`
holds your logins and cookies.

## The layout

```
.flnd/
  metadata/
    flnd.db          what the home remembers: files, posts, targets, runs, failures, …
    flnd.v1.db       a home of version 1, copied whole before its migration (see below)
    secret.db        the vault: logins, cookies and tokens, stored in plaintext
  media/
    <code>/<target>/<category>/…   the downloaded files
  log/
    crash-<time>.txt     the traceback of a crash, for a bug report
    <run>/samples/<n>.txt  the answer a site change was found in, scrubbed (see below)
  config.toml      settings (optional): `[defaults]`, `[plugins.<code>]`
                   (see Settings, below)
  cache/           reserved for things safe to delete; nothing writes there yet
```

`log/` is swept at the start of each run that downloads: an entry older than 30 days goes.
A sample is kept only for a parse failure (`flnd.Changed`) inside a recorded run: values under
keys that look like secrets (`token`, `session`, `cookie`, …) are replaced by "…", an answer
from a login URL or one that sets a cookie is never kept, and the file is named by a count,
never by anything the site sent.

The code: `src/engine/store.py` is `flnd.db` (its schema, the migration of a version-1 home,
its rows and queries, and `check`), and `src/engine/home.py` the rest of the home (a target's
folder, the logs, the logins). They are built on three kit modules: `flnkit/db.py`, a SQLite
store (WAL, one connection shared by threads, transactions), `flnkit/vault.py`, secrets stored
in `secret.db`, and `flnkit/cookies.py`, cookie jars read from files and browsers.

## `media/`

Each target has a folder, `media/<code>/<value>/`, where `<value>` is the target's value made
safe as a file name (`media/ins/example_user/`). Under it, each file goes where the plugin's
`folder` template puts it, `{category}` by default, named by its `filename` template, with the
extension its content calls for:

```
media/ins/example_user/posts/2026-01-02_C1abc_0.jpg
media/ins/example_user/reels/2026-01-03_C2def.mp4
```

Each site's page under [plugins/](README.md#sites) shows its own layout. A file's modified time
is its post's date, at noon UTC, so a file browser sorts by when it was posted.

## Parts

Every file flnd writes, a download or a small file of its own, is first written as a **part**:
`<name>.part` beside it (`posts/2026-01-02_C1abc_0.jpg.part`), renamed onto its real name once
whole. So a file with its real name is always whole. A name too long for the file system once
`.part.json` and the `.part` of that small file's own write are added (255 bytes) gets a
shorter part, its first 180 bytes and an 8-character digest: `<head>.<digest>.part`.

A download's part:

1. is opened and locked before any request is sent (see [Sharing a home](#sharing-a-home));
2. is written as the bytes arrive, from every mirror and retry of the file, and checked through
   the open file: a web page where a photo was expected is removed, never renamed. Its SHA-256
   is computed only when a saved file has its size (dedupe, `files_size`): then it, and each
   such file with no hash yet, is read once on the hasher pool;
3. gets its post's day, at noon UTC, as its modified time, set on the open file just before it
   closes (the rename keeps it); with no post date, the server's `Last-Modified` day;
4. is renamed under the name its content calls for (`a.jpg` holding a WebP lands as `a.webp`);
5. and only then gets its row in `flnd.db`, with the other files of its post. A crash between
   the rename and the row leaves a whole file without a row.

Nothing is flushed to the disk per file (no `fsync`): a power cut may lose the last files, which
the next sync downloads again, but never leaves a half file under a real name.

### Resuming

A download that fails or is stopped keeps its part, and the next run goes on from it:

- it asks the server for the rest, `Range: bytes=<size>-`;
- beside the part, `<name>.part.json` keeps the file's validator, its `ETag` (or its
  `Last-Modified` date when the ETag is weak or missing), and the next run sends it as
  `If-Range`. A file that changed on the server comes whole, and the part starts over from 0,
  so old and new bytes are never spliced;
- a `416` for the part's size means the part is already whole, and any other `416` starts over;
- a big file (16 MiB or more, from a host that takes ranges) may be split into segments while
  its host has room, each written into the part at its offset. The part's `.part.json` then
  keeps the file's `etag`, `last_modified`, `size` and each segment as `[start, end,
  written]`, after every 4 MiB piece is on disk, so the next try or run goes on with each
  segment where it was; a file that changed comes whole instead. It counts only bytes on disk
  (a segment's last piece goes to the disk, never the part's buffer), it is written whole (to
  `<part>.json.part`, then renamed over), one write at a time, and the first new segment starts
  only once it is written, so a crash leaves one a resume can trust. A part an older flnd left
  with `"holes": true` starts over;
- a Telegram document keeps the offsets of its parts in the same `.part.json`, written only
  after the bytes they count are flushed, so a resume never skips bytes the file doesn't hold.

An empty part is removed when its download fails. The `.part.json` is removed when its file
lands.

### Stale parts

When a run starts on a target, it removes the parts under the target's folder that nothing has
touched for 7 days and that no run holds, with their `.part.json`. A `.part.json` without its
part goes after an hour. The hidden `.<name>.stage` files older versions of flnd left go like
parts, and their `.lock` files like a `.part.json`.

## `flnd.db`

One SQLite file in WAL mode, with real columns and indexes, so `sync`, `ls` and the reads of
the library are queries, never walks of `media/`. Read it with any SQLite tool:

```sh
sqlite3 .flnd/metadata/flnd.db "SELECT key, path, size, date FROM files LIMIT 3"
```

How it is written:

- **One transaction per post.** A post's files are downloaded side by side; their rows are
  committed together, in one statement, once the post's last file is in place. A row is never
  written before its file, so a crash leaves at worst a whole file without a row, which the
  next run finds on disk and skips. The `flnd.Saved` events of a post follow its commit, each
  once the steps after its file (unpack, the sidecar) are done.
- **WAL with `synchronous = NORMAL`.** A power cut can lose the last commits, never corrupt the
  file. On macOS, `fullfsync` makes SQLite's own syncs reach the disk. Those syncs happen at
  checkpoints, when the log (`flnd.db-wal`) is copied into the database: every 10,000 pages
  (40 MB), since each full sync costs milliseconds.
- **A 64 MiB page cache.** SQLite's default of 2 MiB holds a tenth of a home of 100,000 files,
  so the read every listed file makes went through the OS for most rows; with the larger cache
  it takes 1.8 to 2.0 µs instead of 2.5. The cache grows only as pages are read.
- **Two runs can share a home** ([Sharing a home](#sharing-a-home)).

### `files`

One row per file, keyed `<code>:<key>`, where `<key>` comes from the plugin's `key` template
(`{post}_{num}` by default): `ins:C1abc_0`. A later run skips a file whose row says it is saved
while the file at its `path` is there, and also a file already on disk at its planned path.

| column | what it holds |
| --- | --- |
| `key` | `<code>:<key>` |
| `target` | the target it was saved for, `<code>:<value>` |
| `plugin` | the plugin's code |
| `post_id`, `category` | its post, and the category it was listed in ("" for a single post) |
| `path` | where the file is, as flnd saved it |
| `size`, `mtime` | its size in bytes, and its modified time in nanoseconds, when saved |
| `date` | its post's day, `YYYY-MM-DD`; empty when the site gave none |
| `estimated` | 1 when the day was guessed from the posts around it |
| `kind` | `image`, `video` or `audio`, by its name |
| `files` | on the first file of a post of several, how many files the post has: it tells a run whether a post listed again is whole |
| `state` | where it stands with the site: `saved`, or `gone` when the site answered 404 or 410; `link` for a second name of a saved file in another category's folder (see [Links](#links)) |
| `at` | when it was saved, in UTC, ISO 8601 |
| `hash` | the SHA-256 of its bytes (32 bytes), taken once a saved file of the same size is met; empty until then, and on a row from before hashes |
| `unpacked` | on an archive: `extracted`, or `failed` (a wrong password, a damaged or unsafe archive, no 7-Zip) |
| `folder` | on an extracted archive: the folder it was extracted into |
| `sidecar` | where its sidecar is, `<path>.json`, when sidecars are on (see [Sidecars](#sidecars)) |

Indexed on `(target, post_id)`, on `size` among the saved files (what dedupe looks up), on
`hash` where there is one, and on the undated rows of each target, so the plugin's `date` hook
finds them without reading the rest. There is no index on
`path` yet: `check` reads every row once instead of looking files up by path, and each index
costs every commit a page.

**Gone files.** When the site answers 404 or 410 for a file (deleted, private, never there), its
row is `gone`: later runs skip it without asking. The CLI's `--again`, or `again = true` under
`[defaults]` in `config.toml` for the library, asks for it again (and lists every post again).

### Dedupe

The same file is often saved twice: a repost under another account, a photo in two albums. When
a file lands, its hash is looked up (one indexed query); if a saved file has the same bytes and
is on disk at the size its row says, the new file is replaced, atomically, by a **hard link** to
it. Both paths stay real files, both rows stay, and the bytes are on disk once. A hard link has
one modified time, so the second file shows the first one's day, and its row says so.

- It is on by default. `dedupe = false` under `[defaults]` in `config.toml` keeps both copies.
- A file system that can't hard-link (FAT and exFAT drives, a link across two volumes) keeps both
  copies, with one warning per run.
- A hard link is one file: editing it in one folder edits it in the other.

### Links

A file listed in two categories of one target (a video that is also a reel) is downloaded once,
into the first category's folder, and the second gets a relative symlink to it (or a hard link
where symlinks are refused). The link has a row of its own, keyed `<file's key>@<category>`, in
state `link`, with the file's hash, so `check` knows it. A `link` row counts toward no post and
no target's files.

### Sidecars

With `sidecars = true` under `[defaults]` in `config.toml` (off by default), each saved file gets
`<file>.json` beside it, with its post's metadata:

```json
{
  "source": "ins",
  "target": "ins:example_user",
  "post": "C1abc",
  "category": "posts",
  "date": "2026-01-02",
  "url": "https://example.com/p/C1abc",
  "file": "https://cdn.example.com/v/1.jpg",
  "caption": "a day out",
  "author": "example_user",
  "tags": ["beach"],
  "fields": {"caption": "a day out", "author": "example_user", "tags": ["beach"]}
}
```

`url`, `caption`, `author` and `tags` are read from the post's fields under the names plugins
give them (`url`, `link` or `permalink`; `caption`, `text`, `description` or `title`; `author`,
`user`, `username`, `owner` or `channel`; `tags` or `hashtags`), and are `null` when the plugin
gives none, as `date` is for a post with no day; `fields` holds every field as the plugin gave
it. The sidecar's `source` key is the plugin's code. `file` is the file's own URL without its
query and user info, which may carry a signature.
The sidecar is written last, so it is named after the final file. Its path is in the row's
`sidecar`.

### `posts`

A view of `files`: one row per post with saved files, by `(target, post_id)`, with its
`category`, its `date`, how many `files` it has, and `at`. It is a view, not a table, so a
post's commit is one statement: a table of its own would cost each commit another statement
and another page.

### `targets`

One row per target the home knows, keyed `<code>:<value>` (`ins:example_user`).

| column | what it holds |
| --- | --- |
| `plugin`, `value`, `kind` | the target: its plugin, its value, and the route that reads it (`user`, `post`, `query`, …) |
| `follow` | 1 when `sync` takes it |
| `categories` | JSON: the categories a followed target syncs, as its text named them (`ins:example_user/reels`); `[]` takes the route's own |
| `query` | for a target a search found: the search's words |
| `complete` | JSON: each category listed to its end, with the UTC day it was |
| `cursors` | JSON: each category's cursor, saved when a full download of it was cut short, so the next run resumes |
| `heads` | JSON: each `newest_first` category's head: the newest post the last run that read it from the top saw, and left no gap; a sync stops after it and the post after it |
| `gaps` | JSON: each category's gaps, newest first: the cursor where a sync of it was cut short, which the next sync jumps to once it meets saved posts on top; `""` is a gap without a cursor (a feed that isn't paged by cursor), which makes the next sync read down to the head |
| `seen` | JSON: for a search, the targets it found, so a sync downloads only new ones |
| `last_sync` | when it was last downloaded or synced, in UTC |
| `files`, `bytes` | its saved files and their bytes, counted at the end of each of its runs; `flnd ls` shows them |
| `account` | the account that last worked for it |

### `runs` and `failures`

`runs` has one row per command that downloads: `id`, `command`, `started`, `ended`, the files
it `saved`, and how many `failed`. A run stopped by Ctrl-C keeps the files it saved before the
stop.

`failures` has one row per failed file, keyed by its `key` however often it fails: the `run`
it last failed in, its `target`, `post_id`, `category` and `file` (where it would have gone),
the `cursor` of the page its post was listed on, the `url` with its query and user info
removed, the error's `class`, `message` and `hint`, `attempts` (the runs that failed it: 1 after
the first, and back to 1 for a file saved since that fails again), and `at`. A failure of a
whole target has the target's key and no post; the target's next run to its end drops it.

No row holds a request to send again, nor a URL with its query: a retry lists the post again
through its source, from `cursor`, for fresh links ([errors.md](errors.md#retrying)). The next
download or sync of the target retries every failed file that has no row in `files` since and
`attempts` of at most 3; `flnd retry` takes every one.

### `state`, `accounts`, `meta`

- `state`: each plugin's own values between runs (`ctx.state`), as JSON, keyed `(plugin, key)`.
  A run reads a plugin's values once, before its first route, keeps the plugin's changes in
  memory, and writes them back as each target ends and as the run ends, always off the loop.
  flnd keeps one entry of its own there too (`src/engine/crawl.py`): `redate.asked`, the posts
  whose date a plugin's `date` hook couldn't find, so they aren't asked again for 30 days.
- `accounts`: each plugin's logged-in account (`name` `default`, `is_default` 1), with how it
  logged in (`origin`: a browser such as `chrome`, `file` for a cookie file, or the plugin's
  own way, `qr`, `phone`), `at`, and its `status`. No secret is ever here. `flnd logout`
  removes the plugin's rows.
- `meta`: the schema's `version`, `2`.

### A home of version 1

A home an earlier flnd made (version 1) moves to version 2 the first time it is opened, in one
transaction: `source` becomes `plugin`; a target's `options` (`only`) become its `categories`;
the `shrink`, `before`, `format` and `tools` columns, and the `probes` table, go; each `logins`
row (and each plugin with a `secret.db` slot but no row) becomes the plugin's default account;
a key that failed more than once keeps its latest failure; and `version` becomes `2`. Before
it starts, the whole file is copied to `metadata/flnd.v1.db`, once, so no row is ever lost: a
crash inside the migration leaves the home at version 1, and the next open migrates it again.
A home of a later version, or one with files and no version, is refused.

## Settings: config.toml

All optional; a home without the file has every default. A setting resolves in this order: a
library argument (or a flag) given for one run, the home's `config.toml`, your user file
(`~/.config/flnd/config.toml`, or `%APPDATA%\flnd\config.toml` on Windows), then the default.
The user file holds what every home shares, and is the only place `exec` is read from.

```toml
[defaults]
dedupe = true        # hard-link a file whose bytes are saved already; false keeps both
sidecars = false     # write <file>.json beside each saved file
when = ""            # the days every download, follow and sync takes, as --when writes them
                     # ("1y", "2026", "2026-01.."); "" for every day
rate = "0"           # the run's speed limit, such as "20 MB/s" or "500 KB/s"; "0" for none
slowest = "0"        # cut a transfer slower than this over 30 s, and go on; "0" for off
# exec = ["notify-send", "{path}"]   # read only from the user file; see the CLI guide


[plugins.ins]        # one table per plugin, by its code
pace = 1.0           # seconds between its page requests (over the plugin's own pace)
limits = { "cdninstagram.com" = 8 }   # the most files at once from a host
proxy = "socks5://127.0.0.1:1080"     # every HTTP request of the plugin goes through it
stop_after = 5       # saved posts in a row that end a sync of a newest-first listing
folder = "{category}"                # the folder and file name templates, over the plugin's
filename = "{date}_{post}{index}"
```

- **`[defaults] rate`** is one byte bucket for every file of the run, a second deep: a file's
  transfer waits after each chunk for the bytes owed. **`slowest`** cuts a transfer that
  averaged under it over its last 30 s; it goes on from where it was, as one of its network
  tries. Before a file of known size starts, its disk must have its size, the bytes still due
  from the files in flight on that disk, and 64 MiB more, or the run stops with a `DiskError`.
- **`[defaults] when`** bounds a sync too, which has no `--when` of its own. Give `--when ""` (or
  `when=""` in the library) for every day in one run.
- **`[plugins.<code>]`** settings win over what the plugin's class says: `pace` over its `pace`
  (0 is no pace), and `limits` merged over its `limits`, so a host named in both takes the
  file's number. A host's cap still adapts under it ([architecture.md](architecture.md#network)).
  `folder` and `filename` replace the class's templates (a category's own `folder` still wins),
  and are checked against the plugin's fields before any request. A plugin's own settings (its
  `Settings`) are keys of its table too, and reach it as `ctx.settings`.
- **`proxy`** takes `http://`, `https://`, `socks5://` or `socks5h://` (which also looks host
  names up through the proxy), with a user and password if the proxy needs them. It carries
  every request and download of the plugin's HTTP session. **The browser isn't proxied**: one
  window serves every plugin of a run, and a plugin's proxy can't be set per tab, so a plugin
  that opens pages (Instagram, TikTok, Facebook, Twitter/X) loads them directly. A run says so
  once, as a warning, when such a plugin opens its first tab.
- **A bad value** stops the command as a usage error that names the file, the key and what it
  takes, never the value written there (it may be a proxy's password):
  `config.toml: plugins.ins.pace is a number of seconds, 0 or more, not this text`. A key flnd
  doesn't know is a warning, said once (`[defaults] compres is not a setting flnd knows; it is
  ignored`), so a typo never passes for a setting that works.
- **Gone:** `[defaults] again` (the CLI's `--again` stays), `[vault] key` and `scroll`.
  `[sources.<code>]` is read as `[plugins.<code>]`, with a warning to rename it, and
  `dedupe = "link"` or `"off"` as `true` or `false`.

The code: `src/engine/config.py` reads, checks and edits both files, in one place.

## The vault: `secret.db`

`secret.db` in `metadata/` has one table, `secrets`: a row per secret of a plugin, keyed
`(slot, name)`, where the slot is the plugin's code. A cookie plugin keeps its jar as `cookies`,
a JSON list of cookies in Playwright's shape (each domain with a leading dot); Telegram keeps its
`session` and its API credentials. Secrets are stored unencrypted in plaintext, so no key file
is needed.

A cookie login (`flnd login <code> chrome`, or a cookie file) keeps only the cookies of the
plugin's domains and their subdomains, and is refused unless one of the cookies the plugin's
login names is there. When a run ends, the cookies the site refreshed are written back into the
jar, for the names already in it. A plain cookie file a much older flnd left,
`cookies/<code>.txt` (or `.json`), is moved into `secret.db` the first time the plugin is used,
and deleted once it is saved.

The code: `flnkit/cookies.py` reads cookies from a file or a browser, `flnkit/vault.py`
stores them in `secret.db`, and `src/engine/home.py` holds the logins that use both.

`flnd logout <code>` removes a plugin's rows.

## Checking a home

`flnd check` (`fl.check()` in a script) compares the history with the disk, and `--fix`
(`fix=True`) makes the history match the disk. It reads every row once, with one `stat` each, and
walks `media/` once. It never deletes, moves or downloads a file, and it never adds a row.

| found | what it means | what `--fix` does |
| --- | --- | --- |
| missing | a row's file isn't there (removed by hand, or a disk lost it) | removes the row, so `flnd download <target> --again` can fetch it again |
| wrong size | a saved file's size isn't its row's (cut short, or changed by another program) | takes the size, time and hash from the disk |
| unknown | a file under `media/` no row names | nothing: it is reported, and stays out of the history |

An unknown file stays unknown: a later run that lists it finds it on disk and skips it, so
nothing is downloaded twice, and `check` reports it each time until you remove it or give it a
row yourself. A home with one isn't all right, fixed or not: `flnd check --fix` repairs the
rows and still exits 1, saying the unknown files stay.


These are never unknown: parts and what sits beside them, hidden files (`.DS_Store`), symlinks,
a hard link of a known file, a known file's sidecar, anything inside the folder an archive was
extracted to, and a file renamed into place in the last minute (another run may be about to
write its row). An archive removed once extracted isn't missing while its folder is there.

## Sharing a home

Two runs may use one home at once: one terminal syncing while another downloads, or cron firing
during a manual run. Nothing makes the second run wait for the first.

- **A part is its own lock.** The run writing it holds an exclusive lock on the open file
  (`flock` on macOS and Linux, `msvcrt.locking` on Windows), taken when the part is opened. A
  second run that finds a part locked skips that file as "being saved by another run"
  (`Skipped` with `reason="busy"`), without sending a request, and the first run writes the
  file's row. The lock goes with the file
  descriptor, so a run that crashes frees it at once.
- A run that opens a part just as another lands it sees that the name now holds another file,
  and skips it too.
- flnd's own small files (a cache, a sidecar) wait for each other instead of skipping.
- `flnd.db` and `secret.db` are SQLite, which serializes writers; a writer waits up to 30 s
  for another run's transaction, which lasts microseconds. A post's commit never waits on the
  run's loop: it is queued again every 20 ms while another process holds the lock, for up to
  30 s, and then the run stops (`DiskError`). Each change of a target's row reads
  and writes it in one transaction, so two runs never write over each other's changes.
- On a file system without locks (some network shares), files are written unlocked.

## Backing up and moving

The home is self-contained: copy the whole folder while no run is going. To start over on a
target, unfollow it; its files and their rows stay, so a new download skips them. To download a
file again, delete it, then download its target: a file whose row's path is missing is
downloaded again.
