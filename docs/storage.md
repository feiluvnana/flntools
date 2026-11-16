# Storage: the home folder

Everything flnd keeps lives in one folder, the **home**: `$FLND_HOME`, else `.flnd` in the folder
flnd runs from; the library takes `flnd.open(home)`. This page lists every file in it and what
each holds, so you can back it up, move it, or read it with your own tools.

Never share a home with someone you wouldn't give your logins to: the vault and its key sit
side by side in it.

## The layout

```
.flnd/
  flnd.db          what the home remembers: files, posts, targets, runs, failures, …
  secrets.db       the vault: logins and tokens, each sealed (AES-256-GCM)
  secret.key       the vault's key (mode 0600)
  config.toml      settings (optional): `[defaults]` again, dedupe, sidecars, ui
  media/
    <code>/<target>/<category>/…   the downloaded files
  cache/           things safe to delete
  logs/            each run's log, once runs write one
```

A home made by flnd before `flnd.db` also has `metadata/` (see [Migrating an old
home](#migrating-an-old-home)).

The code: `flnd/_home.py` is flnd's storage (the layout, `flnd.db`'s tables and queries, the
migration, the logins). It is built on two library modules: `flnd/_db.py`, a SQLite store (WAL,
one connection shared by threads, transactions), and `flnd/_vault.py`, sealed secrets in a
database under a key file.

## `media/`

Each target has a folder, `media/<code>/<value>/`, where `<value>` is the target's value made
safe as a file name (`media/ins/example_user/`). Under it, each file goes where the source's
`folder` template puts it, `{category}` by default, named by its `filename` template, with the
extension its content calls for:

```
media/ins/example_user/posts/2026-01-02_C1abc_0.jpg
media/ins/example_user/reels/2026-01-03_C2def.mp4
```

Each site's page under [sources/](README.md#sites) shows its own layout. A file's modified time
is its post's date, at noon UTC, so a file browser sorts by when it was posted.

## Parts

Every file flnd writes, a download or a small file of its own, is first written as a **part**:
`<name>.part` beside it (`posts/2026-01-02_C1abc_0.jpg.part`), renamed onto its real name once
whole. So a file with its real name is always whole. A name too long for the file system with
`.part.json` added (255 bytes) gets a shorter part, its first 180 bytes and an 8-character
digest: `<head>.<digest>.part`.

A download's part:
1. is opened and locked before any request is sent (see [Sharing a home](#sharing-a-home));
2. is written as the bytes arrive, from every mirror and retry of the file, and checked through
   the open file: a web page where a photo was expected is removed, never renamed. Its SHA-256
   is computed from the same bytes as they are written; a part resumed from an earlier run, or
   written by a tool (ffmpeg joining a DASH video), is read back once instead;
3. gets its post's day, at noon UTC, as its modified time, set on the open file just before it
   closes (the rename keeps it); with no post date, the server's `Last-Modified` day;
4. is renamed under the name its content calls for (`a.jpg` holding a WebP lands as `a.webp`);
5. and only then gets its row in `flnd.db`, with the other files of its post. A crash between the rename and the row leaves a whole
   file without a row.

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
- a big file fetched in 4 ranges at once writes them into the part at their offsets. Until
  they all arrive, the part's `.part.json` says it has holes (`"holes": true`), so a part a
  crash left that way starts over instead of passing for a whole file;
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
sqlite3 .flnd/flnd.db "SELECT key, path, size, date FROM files LIMIT 3"
```

How it is written:

- **One transaction per post.** A post's files are downloaded side by side; their rows are
  committed together, in one statement, once the post's last file is in place. A row is never written before its
  file, so a crash leaves at worst a whole file without a row, which the next run finds on disk
  and skips. The `flnd.Saved` events of a post follow its commit, each once the steps after
  its file (compress, the sidecar) are done.
- **WAL with `synchronous = NORMAL`.** A power cut can lose the last commits, never corrupt the
  file. On macOS, `fullfsync` makes SQLite's own syncs reach the disk. Those syncs happen at
  checkpoints, when the log (`flnd.db-wal`) is copied into the database: every 10,000 pages
  (40 MB), since each full sync costs milliseconds.
- **Two runs can share a home** ([Sharing a home](#sharing-a-home)).

### `files`

One row per file, keyed `<code>:<key>`, where `<key>` comes from the source's `key` template
(`{id}_{num}` by default): `ins:C1abc_0`. A later run skips a file whose row says it is saved
while the file at its `path` is there, and also a file already on disk at its planned path.

| column | what it holds |
| --- | --- |
| `key` | `<code>:<key>` |
| `target` | the target it was saved for, `<code>:<value>` |
| `source` | the source's code |
| `post_id`, `category` | its post, and the category it was listed in ("" for a single post) |
| `path` | where the file is, as flnd saved it |
| `size`, `mtime` | its size in bytes, and its modified time in nanoseconds, when saved |
| `date` | its post's day, `YYYY-MM-DD`; empty when the site gave none |
| `estimated` | 1 when the day was guessed from the posts around it |
| `kind` | `image`, `video` or `audio`, by its name |
| `files` | on the first file of a post of several, how many files the post has: it tells a run whether a post listed again is whole |
| `state` | where it stands with the site: `saved`, or `gone` when the site answered 404 or 410; `link` for a second name of a saved file in another category's folder (see [Links](#links)) |
| `at` | when it was saved, in UTC, ISO 8601 |
| `shrink` | what `--compress` did: `compressed`, `kept` (nothing smaller passed) or `failed`; empty when it never ran ([media.md](media.md)) |
| `tools` | with `shrink`: the encoders and versions it ran with, such as `pillow 13.0.0 libavif 1.4.2`; a `kept` or `failed` file is tried again when these change |
| `before` | on a compressed file: the original's size in bytes (`size` is the new one, and `path` the new path when the suffix changed) |
| `unpacked` | on an archive: `extracted`, or `failed` (a wrong password, a damaged or unsafe archive, no 7-Zip) |
| `folder` | on an extracted archive: the folder it was extracted into. With `--compress` the archive itself is removed, and this folder counts as the saved file |
| `hash` | the SHA-256 of its bytes (32 bytes); after `--compress`, of the compressed file. Empty on a row from before hashes |
| `sidecar` | where its sidecar is, `<path>.json`, when sidecars are on (see [Sidecars](#sidecars)) |
| `format` | for a step that doesn't exist yet; empty |

Indexed on `(target, post_id)`, on `hash` where there is one, and on the undated rows of each
target, so the source's `date` hook finds them without reading the rest. There is no index on
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

- It is on by default. `dedupe = "off"` under `[defaults]` in `config.toml` keeps both copies.
- A file system that can't hard-link (FAT and exFAT drives, a link across two volumes) keeps both
  copies, with one warning per run.
- A hard link is one file: editing it in one folder edits it in the other.
- **Compress keeps the links.** A file and its links are compressed once: the result is linked at
  every path that shared the original (each with the new suffix: `a.jpg` and `b.jpg` become
  `a.avif` and `b.avif`, still one file), and every row moves in one transaction, before the old
  names are removed. A copy with the same bytes that isn't linked to it is left alone.

### Links

A file listed in two categories of one target (a video that is also a reel) is downloaded once,
into the first category's folder, and the second gets a relative symlink to it (or a hard link
where symlinks are refused). The link has a row of its own, keyed `<file's key>@<category>`, in
state `link`, with the file's hash: so `check` knows it, and when compress gives the file a new
suffix, the link is made again pointing at the new name, instead of dangling. A `link` row counts
toward no post and no target's files.

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

`caption`, `author` and `tags` are read from the post's fields under the names sources give
them (`caption`, `text`, `description` or `title`; `author`, `user`, `username`, `owner` or
`channel`; `tags` or `hashtags`), and are `null` when the source gives none; `fields` holds every
field as the source gave it. `file` is the file's own URL without its query, which may carry a
signature. The sidecar is written after compress, so it is named after the final file, and a
later `sync --compress` that renames the file renames its sidecar too. Its path is in the row's
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
| `source`, `value`, `kind` | the target: its source, its value, and the route that reads it (`user`, `post`, `query`, …) |
| `follow` | 1 when `sync` takes it |
| `options` | JSON: `only`, the categories a followed target syncs, and `compress`, whether `sync` compresses its new files (`flnd follow --compress`) |
| `query` | for a target a search found: the search's words |
| `complete` | JSON: each category listed to its end, with the day it was |
| `cursors` | JSON: each category's cursor, saved when a listing was cut short, so the next run resumes |
| `seen` | JSON: for a search, the targets it found, so a sync downloads only new ones |
| `last_sync` | when it was last downloaded or synced, in UTC |
| `files`, `bytes` | its saved files and their bytes, counted at the end of each of its runs; `flnd ls` shows them |

### `runs` and `failures`

`runs` has one row per command that downloads: `id`, `command`, `started`, `ended`, the files
it `saved`, and how many `failed`.

`failures` has one row per failure: the `run`, the file's `key`, its `target`, `post_id` and
`file` (where it would have gone), the `url` with its query and user info removed, the error's
`class`, `message` and `hint`, `attempts`, and `at`. A failure of a whole target has the target's
key and no post. `request` keeps what the next run needs to try the file again from its URL,
with the headers that log a request in (`Cookie`, `Authorization`) removed. An archive's
password is never in `flnd.db`: it is sealed in the vault, as the source's
`password:<key>` secret, and `request` only says that there is one. The next download or sync
of the target tries again every failed file that has no row in `files` since.

### `state`, `logins`, `probes`, `meta`

- `state`: each source's own values between runs (`site.state`), as JSON, keyed `(source,
  key)`. The engine keeps one entry there too: `redate.asked`, the posts whose date a source's
  `date` hook couldn't find, so they aren't asked again for 30 days.
- `logins`: how each source logged in (`origin`: "Chrome", "cookies.txt", "qr") and `at`. No
  secret is ever here.
- `probes`: what ffmpeg found in a file; safe to empty.
- `meta`: the schema's `version`, and `migrated`, when an old home was migrated.

## The vault: `secrets.db` and `secret.key`

`secrets.db` has one table, `secrets`: a row per secret of a source, keyed `(slot, name)`, where
the slot is the source's code. A cookie source keeps its jar as `cookies`; Telegram keeps its
session and API credentials. Each value is sealed with AES-256-GCM under the 32-byte key in
`secret.key`, with `<slot>\0<name>` as associated data, so a value copied to another row doesn't
open. The key file is made on first use with mode 0600.

`flnd logout <code>` removes a source's rows. Deleting `secret.key` makes every saved login
unreadable; log in again after.

## Migrating an old home

A home made before `flnd.db` kept its stores in `metadata/`: `history.db` (every saved file),
`targets.db` (every target, with its cursors and failed files), `sources.db` (each source's
state), `secrets.db` and `secret.key`. They are SQLite tables of JSON values (or JSON files, in
the oldest homes).

The first time flnd opens such a home, it migrates it:

1. It takes `flnd.db`'s write lock (`BEGIN IMMEDIATE`). A second run opening the home at the same
   time waits, then finds the work done.
2. It reads every old store, opened so that nothing on disk changes, and writes their rows into
   `flnd.db`: files keep their paths, and each gets the target whose folder its path is in;
   targets keep whether they were followed (a row from before following existed counts as
   followed), their categories, cursors and found targets; failed files become `failures`.
3. It copies `metadata/secret.key` to `secret.key` and the sealed logins into the new
   `secrets.db`, as they are: the same key opens them, and nothing is decrypted on the way.
4. It records the migration in `meta` and commits. Every later open skips it.

**The old files are never changed or deleted**, so the previous flnd still runs on the home if
you go back to it (it won't see what the new one downloaded). Once you're sure you won't go back,
delete `metadata/`.

## Checking a home

`flnd check` (`fl.check()` in a script) compares the history with the disk, and `--fix`
(`fix=True`) makes the history match the disk. It reads every row once, with one `stat` each, and
walks `media/` once. It never deletes, moves or downloads a file.

| found | what it means | what `--fix` does |
| --- | --- | --- |
| missing | a row's file isn't there (removed by hand, or a disk lost it) | removes the row, so `flnd download <target> --again` can fetch it again |
| wrong size | a saved file's size isn't its row's (cut short, or changed by another program) | takes the size, time and hash from the disk |
| unknown | a file under `media/` no row names | adopts it: gives it a row |

An unknown file is usually one a run renamed into place and then crashed before writing its
row. It is adopted by its `(target, post, file)` key: when a run recorded a failure for that
path, the file takes the failure's key, target and post; else its target is the folder it is in
(`media/<code>/<target>/…`) and its key is `<code>:<target folder>/<path under it>`, numbered
(`#2`) if that key is taken. Either way, a later run that lists the file finds it on disk and
skips it, so nothing is downloaded twice.

These are never unknown: parts and what sits beside them, hidden files (`.DS_Store`), symlinks,
a hard link of a known file, a known file's sidecar, anything inside the folder an archive was
extracted to, and a file renamed into place in the last minute (another run may be about to
write its row). An archive removed once extracted isn't missing while its folder is there.

## Sharing a home

Two runs may use one home at once: one terminal syncing while another downloads, or cron firing
during a manual run. Nothing makes the second run wait for the first.

- **A part is its own lock.** The run writing it holds an exclusive lock on the open file
  (`flock` on macOS and Linux, `msvcrt.locking` on Windows), taken when the part is opened. A
  second run that finds a part locked skips that file as "being saved by another run", without
  sending a request, and the first run writes the file's row. The lock goes with the file
  descriptor, so a run that crashes frees it at once.
- A run that opens a part just as another lands it sees that the name now holds another file,
  and skips it too.
- flnd's own small files (a cache, a sidecar, a cookie file) wait for each other instead of
  skipping.
- `flnd.db` and `secrets.db` are SQLite, which serializes writers; a writer waits up to 30 s
  for another run's transaction, which lasts microseconds. Each change of a target's row reads
  and writes it in one transaction, so two runs never write over each other's changes.
- On a file system without locks (some network shares), files are written unlocked.

## Backing up and moving

The home is self-contained: copy the whole folder while no run is going. To start over on a
target, unfollow it; its files and their rows stay, so a new download skips them. To download a
file again, delete it, then download its target: a file whose row's path is missing is
downloaded again.
