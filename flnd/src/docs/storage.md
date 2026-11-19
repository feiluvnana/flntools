# Storage: the home folder

Everything flnd keeps lives in one folder, the **home**: `$FLND_HOME`, else `.flnd` in the
folder flnd runs from. The library takes `flnd.open(home)`. This page lists what is in it and
what each file holds, so you can back it up, move it, or read it with your own tools.

Never share a home with someone you wouldn't give your logins to: `metadata/secret.db` holds
your logins and cookies.

## The layout

```
.flnd/
  metadata/
    flnd.db          what the home remembers: files, targets, runs, failures, accounts
    flnd.v1.db       a copy of a version-1 flnd.db, made before its migration (see below)
    secret.db        the vault: logins and cookies, each sealed under your key
  media/
    <code>/<value>/<folder>/…   the downloaded files of each target
  log/
    <time>_get.jsonl        the run journal of a `flnd get`: JSON lines
    crash-<time>.txt        the traceback of a bug, for a bug report
    <run>/samples/<n>.txt   the answer a site change was found in, scrubbed (see below)
  config.toml      settings (optional), in the tables [defaults] and [plugins.<code>]
```

`log/` is swept at the start of each run: an entry older than 30 days goes, by its modified
time. A sample is kept only for a parse failure (`flnd.Changed`) inside a recorded run. Values
under keys that look like secrets (`token`, `session`, `cookie`, …) are replaced by "…". An
answer from a login URL, or one that sets a cookie, is never kept. The file is named by a
count, never by anything the site sent.

The code: `src/engine/store.py` holds `flnd.db` (its schema, the migration, its rows and
queries, and `check`) and the rest of the home's paths. `src/engine/auth.py` holds the accounts
and `secret.db`. Both sit on three kit modules: `flnkit/db.py` (the SQLite store), `flnkit/vault.py`
(the sealing) and `flnkit/cookies.py` (cookie jars). Each is described where it is used below.

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
whole. So a file with its real name is always whole. A name too long for the file system, once
`.part.json` and the part of that small file's own write are added (255 bytes), gets a shorter
part: its first 180 bytes and an 8-character digest, `<head>.<digest>.part`.

A download's part:

1. is opened and locked before any request is sent (see [Sharing a home](#sharing-a-home));
2. is written as the bytes arrive, from every mirror and retry of the file. It is checked
   through the open file: a web page where a photo was expected is removed, never renamed. Its
   SHA-256 is computed only when a saved file has its size (dedupe, `files_size`). Then it, and
   each such file with no hash yet, is read once on the hasher pool;
3. gets its post's day, at noon UTC, as its modified time, set on the open file just before it
   closes (the rename keeps it). With no post date, it takes the server's `Last-Modified` day;
4. is renamed under the name its content calls for (`a.jpg` holding a WebP lands as `a.webp`);
5. and only then gets its row in `flnd.db`, with the other files of its post. A crash between
   the rename and the row leaves a whole file without a row.

Nothing is flushed to the disk per file (no `fsync`). A power cut may lose the last files, which
the next download gets again, but never leaves a half file under a real name.

### Resuming

A download that fails or is stopped keeps its part, and the next run goes on from it:

- it asks the server for the rest, `Range: bytes=<size>-`;
- beside the part, `<name>.part.json` keeps the file's validator, its `ETag` (or its
  `Last-Modified` date when the ETag is weak or missing). The next run sends it as `If-Range`.
  A file that changed on the server comes whole, and the part starts over from 0, so old and
  new bytes are never spliced;
- a `416` for the part's size means the part is already whole. Any other `416` starts over;
- a big file (16 MiB or more, from a host that takes ranges) may be split into segments while
  its host has room, each written into the part at its offset. The part's `.part.json` then
  keeps the file's `etag`, `last_modified`, `size` and each segment as `[start, end, written]`,
  after every 4 MiB piece is on disk. The next try or run goes on with each segment where it
  was. A file that changed comes whole instead. The file counts only bytes on disk, and is
  written whole (to `<part>.json.part`, then renamed over), one write at a time. The first new
  segment starts only once that write is done, so a crash leaves one a resume can trust. A part
  an older flnd left with `"holes": true` starts over;
- a Telegram document keeps the offsets of its parts in the same `.part.json`, written only
  after the bytes they count are flushed. A resume never skips bytes the file doesn't hold.

An empty part is removed when its download fails. The `.part.json` is removed when its file
lands.

### Stale parts

When a run starts on a target, it removes the parts under the target's folder that nothing has
touched for 7 days and that no run holds, with their `.part.json`. A `.part.json` without its
part goes after an hour. The hidden `.<name>.stage` files that older versions of flnd left go
like parts, and their `.lock` files like a `.part.json`.

## `flnd.db`

One SQLite file in WAL mode, with real columns and indexes. Reads such as `flnd status`, the
target list and the library's `files()` are queries, never walks of `media/`. Read it with any
SQLite tool:

```sh
sqlite3 .flnd/metadata/flnd.db "SELECT key, path, size, date FROM files LIMIT 3"
```

How it is written:

- **One transaction per post.** A post's files are downloaded side by side. Their rows are
  committed together, in one statement, once the post's last file is in place. A row is never
  written before its file, so a crash leaves at worst a whole file without a row, which the
  next run finds on disk and skips. The `flnd.Saved` events of a post follow its commit, each
  once the steps after its file (unpack, the sidecar) are done.
- **WAL with `synchronous = NORMAL`.** A power cut can lose the last commits, but never corrupt
  the file. On macOS, `fullfsync` makes SQLite's own syncs reach the disk. Those syncs happen at
  checkpoints, when the log (`flnd.db-wal`) is copied into the database. Other connections
  checkpoint by themselves every 10,000 pages (40 MB). The run's own connection checkpoints
  every 1,000 committed rows, and never waits on a lock to do so.
- **A 64 MiB page cache.** SQLite's default of 2 MiB holds a tenth of a home of 100,000 files,
  so the read every listed file makes went through the OS for most rows. With the larger cache
  it takes 1.8 to 2.0 µs instead of 2.5. The cache grows only as pages are read.
- **Two runs can share a home** ([Sharing a home](#sharing-a-home)).

The store is built on `flnkit/db.py`, a small SQLite wrapper that knows no table of its own.
`src/engine/store.py` gives it flnd's schema, and `src/engine/auth.py` gives `secret.db` another
one. Its rules: every statement runs under the store's lock, so threads share one connection.
A transaction takes the write lock when it starts (`BEGIN IMMEDIATE`), so two processes never
read a row, change it and write it back over each other. The second waits up to 30 s
(`BUSY_MS`). A file failure is a `DiskError` that names the file.

From first call to close, a `Db` goes like this:

1. `Db(path, …)` only remembers the path: nothing is opened or made;
2. a read (`one`, `all`) before the file exists returns no row and creates nothing, so a
   command that only looks never leaves an empty database behind;
3. the first write (`write`, `transaction`) opens the file in WAL mode, runs `schema` and then
   `setup`, and keeps the connection;
4. every later call reuses that one connection under the lock. `close` ends it.

```python
from pathlib import Path

from flnkit.db import Db

db = Db(Path("notes.db"), schema="CREATE TABLE IF NOT EXISTS notes (key TEXT, text TEXT)")
with db.transaction() as sql:
    sql.execute("INSERT INTO notes VALUES (?, ?)", ("a", "hello"))
db.one("SELECT text FROM notes WHERE key = ?", ("a",))      # ("hello",)
```

Version 2 of `flnd.db` has these tables. `meta` holds the schema `version`, `2`.

### `files`

One row per file, keyed `<code>:<key>`, where `<key>` comes from the plugin's `key` template
(`{post}_{num}` by default): `ins:C1abc_0`. A later run skips a file whose row says it is saved
while the file at its `path` is there, and also a file already on disk at its planned path.

- `key`: `<code>:<key>`
- `target`: the target it was saved for, `<code>:<value>`
- `plugin`: the plugin's code
- `post_id`, `category`: its post, and the category it was listed in ("" for a single post)
- `path`: where the file is, as flnd saved it
- `size`, `mtime`: its size in bytes, and its modified time in nanoseconds, when saved
- `date`: its post's day, `YYYY-MM-DD`; empty when the site gave none
- `estimated`: 1 when the day was guessed from the posts around it
- `kind`: `image`, `video` or `audio`, by its name
- `files`: on the first file of a post of several, how many files the post has. It tells a run
  whether a post listed again is whole
- `state`: `saved`; `gone` when the site answered 404 or 410; or `link`, a second name of a saved
  file in another category's folder (see [Links](#links))
- `at`: when it was saved, in UTC, ISO 8601
- `hash`: the SHA-256 of its bytes (32 bytes). It is taken once a saved file of the same size is
  met: empty until then, and on a row from before hashes
- `unpacked`: on an archive: `extracted`, or `failed` (a wrong password, a damaged or unsafe
  archive, no 7-Zip)
- `folder`: on an extracted archive: the folder it was extracted into
- `sidecar`: where its sidecar is, `<path>.json`, when sidecars are on (see [Sidecars](#sidecars))

Indexed on `(target, post_id)`, on `size` among the saved files (what dedupe looks up), on
`hash` where there is one, and on the undated rows of each target, so the plugin's `date` hook
finds them without reading the rest. There is no index on `path`: `check` reads every row once
instead of looking files up by path, and each index costs every commit a page.

**Gone files.** When the site answers 404 or 410 for a file (deleted, private, never there), its
row is `gone`, and later runs skip it without asking. The CLI's `--again` asks for it again, and
lists every post again. The library does the same with `flnd.open(home, again=True)`.

### `posts`

A view of `files`: one row per post with saved files, by `(target, post_id)`, with its
`category`, its `date`, how many `files` it has, and `at`. It is a view, not a table, so a
post's commit is one statement. A table of its own would cost each commit another statement
and another page.

### `targets`

One row per target the home knows, keyed `<code>:<value>` (`ins:example_user`).

- `plugin`, `value`, `kind`: the target: its plugin, its value, and the route that reads it (`user`,
  `post`, `query`, …)
- `follow`: 1 when a `get` with no target takes it
- `categories`: JSON: the categories a followed target takes, as its text named them
  (`ins:example_user/reels`); `[]` takes the route's own
- `query`: for a target a search found: the search's words
- `complete`: JSON: each category listed to its end, with the UTC day it was
- `cursors`: JSON: each category's cursor, kept when a full download of it was cut short, so the
  next run resumes
- `heads`: JSON: each `newest_first` category's head: the newest post the last run that read it from
  the top saw, and left no gap. A sync stops after it and the post after it
- `gaps`: JSON: each category's gaps, newest first: the cursor where a sync of it was cut short. The
  next sync jumps to it once it meets saved posts on top. `""` is a gap without a cursor (a feed
  that isn't paged by cursor), which makes the next sync read down to the head
- `seen`: JSON: for a search, the targets it found, so a later sync downloads only new ones
- `last_sync`: when it was last downloaded or synced, in UTC
- `files`, `bytes`: its saved files and their bytes, counted at the end of each of its runs. `flnd
  target list` shows them
- `account`: the account that last worked for it

### `runs` and `failures`

`runs` has one row per command that downloads: `id`, `command`, `started`, `ended`, the files it
`saved`, and how many `failed`. A run stopped by Ctrl-C keeps the files it saved before the stop.

`failures` has one row per failed file, keyed by its `key` however often it fails. It records
the `run` it last failed in, its `target`, `post_id`, `category` and `file` (where it would have
gone), the `cursor` of the page its post was listed on, and the `url` with its query and user
info removed. It also keeps the error's `class`, `message`, `hint` and `retryable` flag,
`attempts` (the runs that failed it: 1 after the first, and back to 1 for a file saved since
that fails again), and `at`. A failure of a whole target has the target's key and no post. The
target's next run to its end drops it.

No row holds a request to send again, nor a URL with its query. A retry lists the post again
through its plugin, from `cursor`, for fresh links ([errors.md](errors.md#retrying)). The next
download or sync of the target retries every failed file that has no row in `files` since, and
whose `attempts` are at most 3. A `NotFound` is never retried. `r` in `flnd target list` makes
every one retryable again.

### `state`, `accounts`

- `state`: each plugin's own values between runs (`ctx.state`), as JSON, keyed `(plugin, key)`.
  A run reads a plugin's values once, before its first route. It keeps the changes in memory,
  and writes them back as each target ends and as the run ends, always off the loop. flnd keeps
  one entry of its own there too (`src/engine/crawl.py`): `redate.asked`, the posts whose date a
  plugin's `date` hook couldn't find, so they aren't asked again for 30 days.
- `accounts`: each plugin's logged-in accounts, one row each, named by the site
  (`Login.whoami`), else as asked in a terminal, else `account1`. A version-1 home names its one
  account `default`. The first is the plugin's default (`is_default` 1). Each row keeps how it
  logged in (`origin`: a browser such as `chrome`, `file` for a cookie file, or the plugin's own
  way, such as `qr` or `phone`), `at`, when its login cookie expires (`expires`), and its
  `status`: `ok`, `held` or `expired`. A `held` account has a `held_until`. No secret is ever
  here. A target runs as the account that worked for it last (`targets.account`), else the
  plugin's first `ok` one. `flnd auth logout` removes every account of the plugin.

### A home of version 1

A home an earlier flnd made (version 1) moves to version 2 the first time it is opened. The
steps are these:

1. Before anything else, the whole file is copied to `metadata/flnd.v1.db`, once, with SQLite's
   online backup. If that copy is already there, it is kept. A failed copy stops the migration.
2. The migration runs in one transaction. The old tables are renamed, the version-2 tables are
   made, and each old table is copied across by column name. A column the new schema lacks is
   dropped. A column the old schema lacks takes its version-1 default.
3. `source` becomes `plugin` in `files`, `targets`, `state`. A target's `options` (`only`)
   become its `categories`.
4. The `logins` table becomes `accounts`: each `logins` row, and each plugin with a `secret.db`
   slot but no row, becomes that plugin's default account. The `probes` table is dropped.
   Then `flnd.engine.auth` seals the slot's values under the account.
5. A key that failed more than once keeps its latest failure.
6. `meta.version` becomes `2`, and the old tables are dropped.

A crash inside the migration leaves the home at version 1, and the next open migrates it again.
A home of a later version, or one with files and no version, is refused.

## Dedupe

The same file is often saved twice: a repost under another account, a photo in two albums. When
a file lands, its hash is looked up (one indexed query). If a saved file has the same bytes and
is on disk at the size its row says, the new file is replaced, atomically, by a **hard link** to
it. Both paths stay real files, both rows stay, and the bytes are on disk once. A hard link has
one modified time, so the second file shows the first one's day, and its row says so.

- It is on by default. `dedupe = false` under `[defaults]` in `config.toml` keeps both copies.
- A file system that can't hard-link (FAT and exFAT drives, a link across two volumes) keeps both
  copies, with one warning per run.
- A hard link is one file: editing it in one folder edits it in the other.

## Links

A file listed in two categories of one target (a video that is also a reel) is downloaded once,
into the first category's folder. The second gets a relative symlink to it, or a hard link where
symlinks are refused. The link has a row of its own, keyed `<file's key>@<category>`, in state
`link`, with the file's hash, so `check` knows it. A `link` row counts toward no post and no
target's files.

## Sidecars

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

`source` is the plugin's code. `date` is the post's day, or `null` when the post has none.
`url`, `caption`, `author` and `tags` are read from the post's fields under the names plugins
give them: `url`, `link` or `permalink`; `caption`, `text`, `description` or `title`; `author`,
`user`, `username`, `owner` or `channel`; `tags` or `hashtags`. Each is `null` when the plugin
gives none. `fields` holds every field the plugin gave, as it gave it. `file` is the file's own
URL with its query and user info removed, since the query may carry a signature.

The sidecar is written after the file is in place and before its post's commit. So its path is
in the row's `sidecar`, and a crash leaves no row without it.

## Settings: config.toml

All settings are optional. A home without the file has every default. A setting resolves in
this order:

1. a library argument, or a CLI flag, given for one run;
2. the home's `config.toml`;
3. your user file: `~/.config/flnd/config.toml`, or `%APPDATA%\flnd\config.toml` on Windows;
4. the default.

The user file holds what every home shares. It is also the only place `exec` is read from. An
`exec` in a home's file is ignored with a warning, and an `exec` given in a call is a usage error.

```toml
[defaults]
dedupe = true        # hard-link a file whose bytes are saved already; false keeps both
sidecars = false     # write <file>.json beside each saved file
when = ""            # the days a get takes, as --when writes them ("1y", "2026",
                     # "2026-01.."); "" for every day
rate = "0"           # the run's speed limit, such as "20 MB/s" or "500 KB/s"; "0" for none
slowest = "0"        # cut a transfer slower than this over 30 s, and go on; "0" for off
height = 0           # the tallest video wanted, in pixels; 0 for the best
switch = false       # move a refused request to the plugin's next account (see below)
# exec = ["notify-send", "{path}"]   # read only from the user file; see the CLI guide


[plugins.ins]        # one table per plugin, by its code
pace = 1.0           # seconds between its page requests (over the plugin's own pace)
limits = { "cdninstagram.com" = 8 }   # the most files at once from a host
proxy = "socks5://127.0.0.1:1080"     # every HTTP request of the plugin goes through it
stop_after = 5       # saved posts in a row that end a sync of a newest-first listing
switch = true        # over [defaults] switch, for this plugin
folder = "{category}"                 # the folder and file name templates, over the
filename = "{date}_{post}{index}"     # plugin's own
```

Every key, and what it takes:

- `[defaults] dedupe`: true or false. Default: `true`.
- `[defaults] sidecars`: true or false. Default: `false`.
- `[defaults] when`: text such as `"1y"`, `"2026"` or `"2026-01..2026-03"`, or `""`. Default: `""`
  (every day).
- `[defaults] exec`: a list: a program and its arguments. User file only. Default: none.
- `[defaults] rate`: a speed such as `"20 MB/s"`, or `"0"` for off. Default: `"0"`.
- `[defaults] slowest`: a speed such as `"50 KB/s"`, or `"0"` for off. Default: `"0"`.
- `[defaults] height`: a whole number of pixels, 0 for the best. Checked, but no engine code reads
  it yet. Default: `0`.
- `[defaults] switch`: true or false. Default: `false`.
- `[plugins.<code>] pace`: seconds between page requests, 0 or more. Default: the plugin's own.
- `[plugins.<code>] limits`: a table of `"<host>" = <files at once, at least 1>`. Default: the
  plugin's own.
- `[plugins.<code>] proxy`: `http://`, `https://`, `socks5://` or `socks5h://` with a host and port.
  Default: none.
- `[plugins.<code>] stop_after`: a whole number of at least 1. Default: `5`.
- `[plugins.<code>] browser`: `"chromium"` or `"chrome"`, for a plugin whose browser is `ANY`.
  Checked, but no engine code reads it yet. Default: none.
- `[plugins.<code>] switch`: true or false, over `[defaults] switch`. Default: the default.
- `[plugins.<code>] folder`: a folder template. Default: the plugin's own.
- `[plugins.<code>] filename`: a file name template. Default: the plugin's own.

A plugin's own settings (its `Settings` struct) are keys of its table too, and reach it as
`ctx.settings`.

- **`[defaults] rate`** is one byte bucket for every file of the run, a second deep. A file's
  transfer waits after each chunk for the bytes owed. **`slowest`** cuts a transfer that
  averaged under it over its last 30 s. It goes on from where it was, as one of its network
  tries. Before a file of known size starts, its disk must have room for its size, the bytes
  still due from the files in flight on that disk, and 64 MiB more. Otherwise the run stops
  with a `DiskError`.
- **`switch`**, on for a plugin with several accounts, moves a request its account can't make to
  the plugin's next account, in `auth list` order. That account must not have run the request
  yet, must not be expired, and must not be held. This covers a block, a login the site refused
  (the account is marked `expired`), `Hidden`, and a target's first page that is `NotFound`. A
  blocked account is held for the block's wait (at least 30 s), and marked `held` until then. A
  request never goes back to an account it left. Once it has run as every one, it waits or fails
  as it would without switching. The account a target ran as to its end is the one it starts as
  next time. Each move is a `switch` record in the run's journal, and the `now` line shows it.
- **`[defaults] when`** bounds a sync too, which has no `--when` of its own. Give `--when ""` (or
  `when=""` in the library) for every day in one run.
- **`[plugins.<code>]`** settings win over what the plugin's class says: `pace` over its `pace`
  (0 is no pace), and `limits` merged over its `limits`, so a host named in both takes the
  file's number. A host's cap still adapts under it ([architecture.md](architecture.md#network)).
  `folder` and `filename` replace the class's templates (a category's own `folder` still wins).
  They are checked against the plugin's fields before any request.
- **`proxy`** carries every request and download of the plugin's HTTP session. `socks5h://`
  also looks host names up through the proxy. A proxy URL may hold a user and password.
  **The browser isn't proxied**: one window serves every plugin of a run, and a plugin's proxy
  can't be set per tab. So a plugin that opens pages (Instagram, TikTok, Facebook, Twitter/X)
  loads them directly. A run says so once, as a warning, when such a plugin opens its first tab.
- **A bad value** stops the command as a usage error. The error names the file, the key and what
  the key takes, never the value written there, since it may be a proxy's password:
  `config.toml: plugins.ins.pace is a number of seconds, 0 or more, not this text`. A key flnd
  doesn't know is a warning, said once, and is ignored. A typo never passes for a setting that
  works.
- **Gone:** `[defaults] again` (the CLI's `--again` and the library's `again=` stay), and
  `[vault]` with its `key`, and `scroll`. They are unknown keys now, so they get the warning above.
  `[sources.<code>]` is read as `[plugins.<code>]`, with a warning to rename it. `dedupe = "link"`
  reads as `true`, and `dedupe = "off"` as `false`, each with a warning.

The code: `src/engine/config.py` reads, checks and edits both files in one place. `flnd config`
sets a key, and writes the file with its comments kept.

## The vault: `secret.db`

`secret.db` in `metadata/` has one table, `secrets`:

```sql
CREATE TABLE IF NOT EXISTS secrets (
    account INTEGER NOT NULL, name TEXT NOT NULL, sealed BLOB NOT NULL,
    PRIMARY KEY (account, name)) WITHOUT ROWID
```

A row is one secret of an account, keyed `(account, name)`, where the account is a row of
`flnd.db`'s `accounts`. A cookie plugin keeps its jar as `cookies`, a JSON list of cookies in
Playwright's shape. Telegram keeps its `session` and its API credentials.

**Sealing.** Each value is sealed with AES-256-GCM (`flnkit/vault.py`). A sealed value is a
version byte (1), a 12-byte nonce, then the ciphertext and its 16-byte tag. Its context is its
`(account, name)`, so a value copied to another row doesn't open. The key is 32 random bytes:

- in `~/.config/flnd/key` on Linux and macOS, or `%APPDATA%\flnd\key` on Windows;
- in a file of mode 0600, in a folder made 0700;
- one per user, shared by every home, made by the first write (the first login);
- never inside a home. A key file inside a home is refused, so a copy of a home carries no key.

**A missing key.** With the key file missing or changed, reading a sealed value raises "the
vault's key is gone", with a hint to log in again. The rows are kept, so they open again once
the key file is back. A new login replaces them. A run that finds the key gone marks the account
`expired`, and `flnd auth list` shows it so.

**Older files.** A `secret.db` of an older flnd, with plaintext values, is sealed in place the
first time it is opened. Each plugin's values go under its default account.

**Logins.** A cookie login (`flnd auth login <code> chrome`, or a cookie file) keeps only the
cookies of the plugin's domains and their subdomains. It is refused unless one of the cookies the
plugin's login names is there. Any login must pass the plugin's `Login.check` before it is kept.
A plugin's own login (`qr`, `phone`) may write its secrets only while its `Login.start` runs.
Logging in again as an account that is there replaces its login. When a run ends, the cookies
the site refreshed are written back into its account's jar, for the names already in it.
`flnd auth logout <code>` removes every account of the plugin, and its secrets.

**Reading cookies.** `flnkit/cookies.py` makes the jars. A `Jar` is an immutable collection of
cookies in Playwright's dictionary shape (`name`, `value`, `domain`, `path`, `expires`, `secure`,
`httpOnly`, `sameSite`). It filters by domain and name, reads a value by name, builds a `Cookie:`
header, and saves itself as a Netscape `cookies.txt`. `load` reads a Netscape or JSON cookie file,
raw cookie text, or the cookie database of an installed browser. Only two browsers are read:
Firefox, and Safari (on macOS). `load` refuses Chromium-family browsers and names the Chrome login
instead. That login goes through flnd's Chrome extension. `parse_binarycookies` reads Safari's
`Cookies.binarycookies` format. The login code in `src/engine/auth.py` loads a jar at
`flnd auth login` and saves it in the vault. The HTTP client (`flnkit/net.py`) and the browser
(`flnkit/browser.py`) send it.

To read a browser, `load` finds the platform's profile folder, and copies the cookie database
into memory with SQLite's backup API, read-only. A running browser's lock or write-ahead log
never blocks the read or gets corrupted by it. Then it reads every row.

Rules a change must keep:

- A cookie's value is a login. It is never printed, logged, put in an error message or a
  `repr`. Messages name the file, the line number, the browser or the cookie's name only.
- A `Jar` never changes after it is made: every method returns a new jar or a copy.
- A saved file is written atomically with mode 0600, so a crash never leaves half a login.

## Checking a home

`flnd status` (`fl.check()` in a script) compares the history with the disk. A repair
(`fix=True`) makes the history match the disk. It reads every row once, with one `stat` each, and
walks `media/` once. It never deletes, moves or downloads a file, and it never adds a row.

- **missing**: a row's file isn't there (removed by hand, or a disk lost it). A repair
  removes the row, so `flnd get <target> --again` can fetch it again.
- **wrong size**: a saved file's size isn't its row's (cut short, or changed by another
  program). A repair takes the size, time and hash from the disk.
- **unknown**: a file under `media/` no row names. Nothing is done: it is reported, and stays
  out of the history.

An unknown file stays unknown. A later run that lists it finds it on disk and skips it, so
nothing is downloaded twice. `status` reports it each time until you remove it or give it a row
yourself. A home with one isn't all right, fixed or not: a repair fixes the rows, and
`flnd status` still exits 1, saying the unknown files stay.

These are never unknown: parts and what sits beside them, hidden files (`.DS_Store`), symlinks,
a hard link of a known file, a known file's sidecar, anything inside the folder an archive was
extracted to, and a file renamed into place in the last minute (another run may be about to
write its row). An archive removed once extracted isn't missing while its folder is there.

## Sharing a home

Two runs may use one home at once: one terminal downloading while another runs, or cron firing
during a manual run. Nothing makes the second run wait for the first.

- **A part is its own lock.** The run writing it holds an exclusive lock on the open file
  (`flock` on macOS and Linux, `msvcrt.locking` on Windows), taken when the part is opened. A
  second run that finds a part locked skips that file as "being saved by another run"
  (`Skipped` with `reason="busy"`). It sends no request for that file, and the first run writes
  the file's row. The lock goes with the file descriptor, so a run that crashes frees it at once.
- A run that opens a part just as another lands it sees that the name now holds another file,
  and skips it too.
- flnd's own small files (a cache, a sidecar) wait for each other instead of skipping.
- `flnd.db` and `secret.db` are SQLite, which serializes writers. A writer waits up to 30 s for
  another run's transaction, which lasts microseconds. A post's commit never waits on the run's
  loop. It is queued again every 20 ms while another process holds the lock, for up to 30 s, and
  then the run stops (`DiskError`). Each change of a target's row reads and writes it in one
  transaction, so two runs never write over each other's changes.
- On a file system without locks (some network shares), files are written unlocked.

## Backing up and moving

The home is self-contained: copy the whole folder while no run is going. A copy holds the
logins, so keep it as private as the home. The key is not in the home, so the copy opens only
with the key on the machine it is opened on. To start over on a target, unfollow it. Its files
and their rows stay, so a new download skips them. To download a file again, delete it, then
download its target: a file whose row's path is missing is downloaded again.
