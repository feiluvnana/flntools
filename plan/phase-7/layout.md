# layout

Phase 7, step 1, **built** 2026-11-13. The user asked on 2026-11-13 for `.flnd/` to hold three folders: the media, the
stores, and a log of each run to audit later.

## Today

```
.flnd/
  downloads/<source>/<target>/…     the media (flnd/common/files.py, engine.py)
  history.db                        every saved file by key (engine.py)
  targets.db                        each target's saved settings and complete listings (settings.py)
  sources.db                        each source's own state, a table per source (site.py)
  secrets.db, secret.key            encrypted logins and their key (common/auth.py)
  cookies/                          only Phase 5 logins not yet moved into secrets.db (common/auth.py)
```

## After

```
.flnd/
  media/<source>/<target>/…         renamed from downloads/; the layout inside is unchanged
  metadata/
    history.db
    targets.db
    sources.db
    secrets.db
    secret.key                      beside the store it opens
  log/
    2026-11-13_143005_download.log  one file per run
```

- **One constant per folder** in the engine (`MEDIA`, `METADATA`, `LOGS`), and every module takes
  its path from them: `files.target_folder`, `settings`, `site`'s source state and `auth` stop
  joining names onto `home` themselves.
- `cookies/` stays where it is: it exists only until an old login moves into `secrets.db`, and
  then it is removed, as today.
- The `Storage:` line names `.flnd/media/`. README's "Download Layout" becomes "Media Layout".

Step 10 renames `sources.db` to `sites.db`, as "source" leaves flnd's vocabulary
([flnd](flnd.md#4-sites-site-downloader-finder)); with no migration, a site's learned pace and
state start fresh once, and loi's record of the posts it took today with it.

## No move

Decided by the user on 2026-11-13: **no migration.** flnd reads and writes only the new layout. An
old `.flnd/` is left as it is; README says which folder goes where for a person who wants to move
theirs by hand. Rows keep their path **relative to `media/`**, so a later move needs no rewrite.

## Run logs

Every run of `download` and `sync` writes `.flnd/log/<date>_<time>_<command>.log`: **every line
`-v` shows, at debug, whatever the screen's level**, so a run can be audited later without having
been run with `-v`.
- **Plain text**, no colors, no live area: one line per event, each with its time:
  `14:30:07.412 ✓ 45.4 KB fbk:example.page posts/2026-11-11_122168984774665018.jpg`.
- **Phases** are marked so a reader can find their place: the run's start (version, command, the
  options as given, targets), then per target `resolve`, `repair` (sync), `list <category>`,
  `encode`, `link`, its summary; then the totals and the exit status. A status line's counts are
  written when its category ends, not every frame.
- **Warnings and errors with their cause**: a failed file's exception chain, a rate limit's
  headers (`retry-after`, the site's rate headers), so a block can be understood afterwards.
- **Nothing secret**, as everywhere: no cookie, key, password, session string or `Authorization`
  header. URLs are written **without their query string**, since a CDN's signed URL is a
  credential while it lasts.
- **`shared.log` gains a second destination**: `log.record(path)` adds a file that takes every
  line at debug, unpainted, with a time; `log.level` still decides only the screen.
- **Kept 30 days** (user, 2026-11-13), then deleted at the start of a run. `login` and `logout`
  write no log.

## Tests

Every store opens under `metadata/` and every file lands under `media/`; a run writes a log with its phases at debug while the screen
is at info; a URL's query string and a cookie value never reach the log file; logs older than the
limit are deleted.

## As built (2026-11-13)

- The table is in `flnd/common/files.py` (lean E9): `MEDIA`, `METADATA`, `LOGS`, and `HISTORY`,
  `TARGETS`, `SOURCE_STATE`, `SECRETS`, `KEY`, each a path relative to `.flnd/`.
- `log.record(path)` is a context manager around the run. It also takes the rows' final lines
  (whatever `quiet` hides), and cuts the query string and fragment off every `scheme://` URL in a line.
- Two runs started in the same second get `…_download.log` and `…_download-2.log`.
- Phase marks so far: start, `resolve`, `repair`, `list <category>`, each target's summary,
  totals and exit status (2 usage error, 130 Ctrl-C, 1 failure). `encode` and `link` come with
  their steps. A 429's `retry-after` and rate headers, and a failure's causes, are debug lines.
