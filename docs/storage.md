# Storage: the home folder

Everything flnd keeps lives in one folder, the **home**. Today it is `.flnd` in the folder flnd
runs from; the library calls take `home=` to use another. This page lists every file in it and
what each holds, so you can back it up, move it, or read it with your own tools.

The rebuild moves the history, targets and runs into one `flnd.db` with real columns, and
migrates an old home on first open without changing its files. That isn't built yet; the commit
that builds it rewrites this page and adds the migration.

Never share a home with someone you wouldn't give your logins to: the vault and its key sit
side by side in it.

## The layout

```
.flnd/
  media/
    <code>/<target>/<category>/…   the downloaded files
  metadata/
    history.db                     every saved file, by history key
    targets.db                     every target's settings, cursors and failures
    sources.db                     each source's own state
    secrets.db                     logins, encrypted
    secret.key                     the key to secrets.db (mode 0600)
```

There is no lock file anywhere: each file being written locks itself (see
[Sharing a home](#sharing-a-home)).

`paths.LOGS` names a `log/` folder, but nothing writes there yet: today a run logs to the
terminal only (`-v` for debug lines).

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
   the open file: a web page where a photo was expected is removed, never renamed;
3. gets its post's day, at noon UTC, as its modified time, set on the open file just before it
   closes (the rename keeps it); with no post date, the server's `Last-Modified` day;
4. is renamed under the name its content calls for (`a.jpg` holding a WebP lands as `a.webp`);
5. and only then gets its history row. A crash between the rename and the row leaves a whole
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

## The stores

Each `.db` file is an SQLite database in WAL mode, made by `flnd._db`. A store holds named
tables, and every table has the same two columns: `key` (text, the primary key) and `value`
(JSON text). Read one with any SQLite tool:

```sh
sqlite3 .flnd/metadata/history.db "SELECT key, value FROM t_downloads LIMIT 3"
```

A table's SQL name is its name with a `t_` prefix.

### `history.db`, table `downloads`

One row per saved file. The key is `<code>:<key>`, where `<key>` comes from the source's `key`
template (`{id}_{num}` by default): `ins:C1abc_0`. A later run skips a file whose key is here
while the file at its `path` still exists, and also a file already on disk at its planned path.
The value:

| field | what it holds |
| --- | --- |
| `path` | the file's path as flnd saved it, under the home |
| `size` | its size in bytes |
| `mtime` | its modified time, in nanoseconds, when it was saved |
| `at` | when it was saved, in UTC, ISO 8601 |
| `id` | its post's id |
| `category` | the category it was listed in; empty for a single post |
| `date` | its post's day, `YYYY-MM-DD`; absent when the post had none |
| `estimated` | true when the day was guessed from the posts around it |
| `files` | on a post's first file, how many files the post has, when more than one |
| `shrink` | what `--compress` did: `compressed`, `kept` (nothing smaller passed) or `failed`; absent when it never ran ([media.md](media.md)) |
| `tools` | with `shrink`: the encoders and versions it ran with, such as `pillow 13.0.0 libavif 1.4.2`; a `kept` or `failed` file is tried again when these change |
| `before` | on a compressed file: the original's size in bytes (`size` is the new one, and `path` the new path when the suffix changed) |
| `unpacked` | on an archive: `extracted`, or `failed` (a wrong password, a damaged or unsafe archive, no 7-Zip) |
| `folder` | on an extracted archive: the folder it was extracted into. With `--compress` the archive itself is removed, and this folder counts as the saved file |

### `targets.db`, table `targets`

One row per target ever downloaded, keyed `<code>:<value>` (`ins:example_user`). `flnd target`
reads and changes these rows. The value:

| field | what it holds |
| --- | --- |
| `kind` | the route that read it: `user`, `post`, `chat`, … |
| `sync` | whether `flnd sync` updates it |
| `at` | when it was last recorded, in UTC |
| `complete` | each category listed to its end, with the day it was |
| `cursors` | each category's cursor, saved when a listing was cut short, so the next run resumes |
| `failed` | the files that failed, by history key, with what is needed to try them again |
| `seen` | for a search: the targets it has found, so a sync downloads only new ones |
| `query` | for a target found by a search: the search's text |

### `sources.db`

One table per source, named by its code, which the source uses as `site.state`: things worth
keeping between runs, such as a resolved user id. The engine keeps one entry there too:
`redate.asked`, the posts whose date a source's `date` hook couldn't find, so they aren't asked
again for 30 days.

### `secrets.db` and `secret.key`

The vault, table `secrets`. Each row is one secret of one source, keyed `<code>:<name>`: a
cookie source's jar (`cookies`), how it logged in (`origin`), or Telegram's session and API
credentials. Each value is sealed with AES-GCM under the 32-byte key in `secret.key`, bound to
its own key, so a value copied to another row doesn't decrypt. The key file is made on first use
with mode 0600.

`flnd logout <code>` removes a source's rows. Deleting `secret.key` makes every saved login
unreadable; log in again after.

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
- The stores are SQLite, which serializes writers; a writer waits up to 30 s for another.
- Two runs that record the same target in `targets.db` at once each write their own view of it,
  and the last one wins.
- On a file system without locks (some network shares), files are written unlocked.

## Backing up and moving

The home is self-contained: copy the whole folder while no run is going. To start over on a
target, `flnd unfollow` it; its files and history stay, so a new download skips them. To
download a file again, delete it, then download its target again: a file that is not on disk
is fetched again.
