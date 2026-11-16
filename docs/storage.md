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
    .lock                          held while a download or sync changes targets.db
    locks/<hash>.lock              one per target folder, held while it downloads
```

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

A file is written as a hidden stage file beside it, `.<name>.stage`, and renamed into place once
complete, so a file with its real name is always whole. The next run on a target removes the
stale stages an interrupted run left in its folder.

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

`flnd auth logout` removes a source's rows. Deleting `secret.key` makes every saved login
unreadable; log in again after.

## Locks and sharing a home

- `metadata/.lock` is held by `flnd download` and `flnd sync` while they record targets, so two
  runs on one home take turns there.
- Each target's folder has its own lock in `metadata/locks/`, so two runs never download the same
  target at once; the second waits.
- Every stage file is locked by the run writing it.
- The stores are SQLite, which serializes writers; a writer waits up to 30 s for another.

## Backing up and moving

The home is self-contained: copy the whole folder while no run is going. To start over on a
target, `flnd target del` it; its files and history stay, so a new download skips them. To
download a file again, delete it, then download its target with `--again`, so the listing
doesn't stop at saved posts before reaching it.
