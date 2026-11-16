# The command line

Every command and flag `flnd` has today, with its default and an example. `flnd --help`, and
`--help` after any command, print the same from the code.

The rebuild replaces several commands (`flnd follow`, `flnd ls`, `flnd login`, `--only`,
`--when`); this page changes in the same commit as the CLI does. Until then, the commands below
are the ones that work.

## Running it

From a checkout, `make setup` creates the environment, then:

```sh
uv run flnd --help
uv run flnd download ins:example_user --from posts
```

flnd keeps everything in `.flnd` in the **current folder** (see [storage.md](storage.md)), so
run it from the folder that should hold your downloads.

| flag | what it does |
| --- | --- |
| `--help`, `-h` | print the commands, the target forms of every source, and their categories |
| `--version`, `-V` | print flnd's version |

## Targets

A target is `<source>:<name, id or link>`, `<source>?<words>` for a search, or a bare link:

```sh
flnd download ins:example_user --from posts
flnd download https://www.instagram.com/example_user/ --from posts,reels
flnd download "mht?example words" --from chapters
flnd download https://www.tiktok.com/@example_user/video/1234567890
```

`flnd --help` lists every form each source accepts. The pages under [sources/](README.md#sites)
explain them site by site.

## `flnd download`

Download each target once.

```sh
flnd download <targets…> --from CATEGORIES [--time SINCE,UNTIL] [--sync] [--again]
              [--limit N] [--dry] [-v]
```

| flag | default | what it does |
| --- | --- | --- |
| `<targets…>` | standard input | the targets; with none, flnd reads them from standard input, one per line, skipping blank lines and lines starting with `#` |
| `--from` | none | the categories to download, such as `posts,reels`, or `all`. **Required** for a profile, a chat or a search; refused for a single post, which is always downloaded whole. With targets from several sources, `ins:posts,reels` scopes a list to one source |
| `--time` | no limit | a date range, `SINCE,UNTIL`, both days included, written `YYYY-MM-DD`; `*` or an empty side means no limit (`2026-01-01,` or `*,2026-06-30`) |
| `--sync` | off | remember the targets as synced, so `flnd sync` updates them |
| `--again` | off | ignore saved cursors and the "stop at saved posts" rule, and list every post from the start |
| `--limit` | no limit | at most N posts per target, or at most N targets found by a search; at least 1 |
| `--dry` | off | list what would be downloaded (files, or the targets a search finds) and write nothing |
| `-v`, `--verbose` | off | show debug lines and every file's progress |

Examples:

```sh
flnd download ins:example_user --from all
flnd download ins:example_user --from reels --time 2026-01-01,2026-06-30
flnd download tik:example_user twt:example_user --from tik:videos twt:media --sync
flnd download "mht?example" --from chapters --limit 5 --dry
cat targets.txt | flnd download --from all
```

A target already synced is downloaded as a sync (stopping at saved posts) unless `--again` is
given. A target downloaded without `--sync`, and not already synced, gets a hint at the end that
`flnd target sync-on` would keep it up to date.

## `flnd sync`

Download what is new for synced targets.

```sh
flnd sync [targets…] [-v]
```

| flag | default | what it does |
| --- | --- | --- |
| `[targets…]` | every synced target | only these targets |
| `-v`, `--verbose` | off | show debug lines and every file's progress |

A sync lists each target's categories newest first and stops a category after 5 saved posts in
a row, so a sync with nothing new takes a few requests per target. A synced search downloads
only the targets it hadn't found before. Each target keeps the `--from` and `--time` it was
first downloaded with.

## `flnd target`

Targets flnd has downloaded before, and whether `flnd sync` updates them. They are the rows of
`metadata/targets.db`.

| command | what it does |
| --- | --- |
| `flnd target ls` | a table of every known target: sync on or off, its files and size (or, for a search, how many targets it found), when it last ran, and each category's state |
| `flnd target sync-on <targets…>` | turn syncing on for known targets |
| `flnd target sync-off <targets…>` | turn syncing off; the target, its files and its history stay |
| `flnd target del <targets…>` | forget targets: their rows go, their downloaded files and history stay |

```sh
flnd target ls
flnd target sync-off ins:example_user
```

## `flnd auth`

Logins. Each source's login is stored encrypted in the home's vault.

| command | what it does |
| --- | --- |
| `flnd auth login` | log in to a source, asking which one and how (in a terminal only) |
| `flnd auth logout <sources…>` | remove the saved logins of these sources, or of `all` |
| `flnd auth ls` | a table of every source: logged in or not, how it logged in, and the login cookies that expire soon |

`flnd auth login` offers each source's own ways to log in:

- **From your browser:** flnd reads the site's cookies from an installed browser, such as Chrome
  or Firefox, and keeps only those of the site's domains.
- **From a cookie file:** a Netscape `cookies.txt` or a JSON export, given by its path.
- **With a QR code** or **with your phone number and a code:** Telegram's own login.

A login is checked before it is saved: the cookies must hold one of the source's login cookies.
See each site's page for which cookies those are.

```sh
flnd auth login
flnd auth logout ins
flnd auth logout all
flnd auth ls
```

## Exit codes

| code | meaning |
| --- | --- |
| 0 | everything worked |
| 1 | some files or targets failed (each is named above the exit); flnd went on with the rest |
| 2 | a usage error: an unknown flag, a target no source reads, a bad `--from`, `--time` or `--limit` |
| 3 | a login is needed: a source isn't logged in, its login was refused, or `flnd auth login` failed |
| 4 | flnd itself stopped: a full or refused disk, a missing tool, a broken source, or a bug |
| 130 | stopped with Ctrl-C |

A cron job can tell "some files failed" (1) from "flnd broke" (4). Each error class has its
code; [errors.md](errors.md) lists them.

A mistake you can fix (usage, login, disk, a tool) prints one line saying what went wrong, then
one line saying what to do, and never a traceback. A bug prints one line naming a crash file,
`.flnd/logs/crash-<time>.txt`, which holds the traceback; with `-v` the traceback prints
instead.

## Ctrl-C

- The **first** Ctrl-C stops listing and lets the files in progress finish.
- The **second** stops at once. Unfinished files stay as parts (`<name>.part`), which the next
  run resumes, and it resumes each category from its saved cursor.

## For contributors

`make try ARGS="<target> [--from …] [--time …]"` lists, live, the files one
target would save, with each file's planned path, and downloads and records nothing
(`flnd/dry.py`). It uses the login saved in the current folder's `.flnd`.
