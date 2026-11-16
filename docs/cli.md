# The command line

Every command and flag `flnd` has, with its default and an example. `flnd --help`, and `--help`
after any command, print the same from the code: each command's help is its function's
docstring in `flnd/cli.py`.

The CLI is a thin layer over the library ([library.md](library.md)): each command makes one
library call, and a run's events become lines on the screen. Anything the command line does, a
script can do too.

## Running it

From a checkout, `make setup` creates the environment, then:

```sh
uv run flnd --help
uv run flnd download ins:example_user --only posts
```

`flnd --help` lists the commands, then every form of target each source reads and its
categories. It imports no source, so it answers in a few tens of milliseconds.

## Global flags

These go before or after the command: `flnd --home ~/Media/flnd sync` and
`flnd sync --home ~/Media/flnd` are the same.

| flag | default | what it does |
| --- | --- | --- |
| `--home DIR` | `$FLND_HOME`, else `./.flnd` | the home: downloads, history and logins ([storage.md](storage.md)) |
| `--ui plain\|inline` | `inline` on a terminal, else `plain` | how a run shows: one line per file, or a live view of each target and category |
| `-v`, `--verbose` | off | show flnd's debug lines too, and a bug's traceback instead of a crash file |
| `--json` | off | print events and rows as JSON lines, for scripts and cron; implies `--ui plain`, and never asks a question |
| `--help`, `-h` | | print the help of flnd, or of the command it follows |
| `--version`, `-V` | | print flnd's version |

A flag given on the command line wins; then the environment (`FLND_HOME`); then the default.

## Targets

A target is `<source>:<name, id or link>`, `<source>?<words>` for a search, or a bare link:

```sh
flnd download ins:example_user
flnd download https://www.instagram.com/example_user/ --only posts,reels
flnd download "mht?example words" --only chapters
flnd download https://www.tiktok.com/@example_user/video/1234567890
```

`flnd --help` and `flnd sources` list every form each source accepts. The pages under
[sources/](README.md#sites) explain them site by site.

## `flnd download`

Download what the targets have now, once. Nothing is followed: a later `flnd sync` doesn't take
these targets (`flnd follow` does).

```sh
flnd download <targets…> [--only CATS] [--when WHEN] [--limit N] [--compress] [--dry] [--exec CMD]
```

| flag | default | what it does |
| --- | --- | --- |
| `<targets…>` | standard input | the targets; with none, flnd reads them from standard input, one per line, skipping blank lines and lines starting with `#` |
| `--only CATS` | every category | only these categories, such as `reels,posts`; may be given more than once. With targets from several sources, `ins:reels` names one source's. A single post has no categories, so `--only` with one is a usage error |
| `--when WHEN` | every day | only posts from these days: a year `2026`, a month `2026-06`, a day `2026-06-15`, a range `2026-01..2026-03` (either end may be left out), or a span back from today `30d`, `8w`, `1y` |
| `--limit N` | no limit | at most N posts per target, or N targets found by a search; at least 1 |
| `--compress` | off | replace each saved photo and video by a smaller copy that looks the same, when one passes every check, and remove each archive once it is extracted ([media.md](media.md)) |
| `--dry` | off | list what would be downloaded (each file `new` or `saved`, or the targets a search finds), and download and write nothing |
| `--exec CMD` | none | run a command for each saved file, without a shell. Not built yet: a usage error |

Archives (`.zip`, `.7z`, `.rar`, `.tar*`) are always extracted beside themselves, into a folder
of their name; without `--compress` the archive is kept too. 7z and RAR need 7-Zip (`7zz`), and
compressing a video needs ffmpeg, both on your PATH ([media.md](media.md)).

Examples:

```sh
flnd download ins:example_user
flnd download ins:example_user --only reels --when 2026-01..2026-06
flnd download tik:example_user twt:example_user --only tik:videos --only twt:media
flnd download "mht?example" --only chapters --limit 5 --dry
flnd download ins:example_user --only posts --compress
cat targets.txt | flnd download
```

A file already saved is skipped, so downloading a target again fetches only what is new.

## `flnd follow`

Follow targets, so `flnd sync` keeps them up to date, and download what they have now.

```sh
flnd follow <targets…> [--only CATS] [--when WHEN] [--compress]
```

`--only` is kept with each target for later syncs; following again replaces it. `--when` bounds
this first download only. The flags read as `download`'s do.

## `flnd sync`

Download what is new for every followed target, or for the ones named.

```sh
flnd sync [targets…] [--compress] [--exec CMD]
```

Each target keeps the `--only` it was followed with. A sync lists each category newest first and
stops it after a few saved posts in a row, so a sync with nothing new costs a few requests per
target. A followed search downloads only the targets it hadn't found before.

`--compress` compresses the new files as they land, then the files these targets saved
before that aren't done yet; a file compressed, or kept with the same tools, is skipped
without being opened ([media.md](media.md)).

## `flnd unfollow`

Stop following targets: `flnd sync` leaves them out. Their files and history stay.

```sh
flnd unfollow ins:example_user
```

## `flnd ls`

List the targets this home knows, followed or not: their files and size (or, for a search, how
many targets it found), when they last ran, and each category's state.

```sh
flnd ls
flnd ls ins:example_user --json
```

## `flnd status`, `flnd retry`, `flnd check`

Not built yet; each is a usage error for now.

- `flnd status` will show the last run: what failed and why. Today the end of each run lists its
  failures.
- `flnd retry` will download again the files the last run failed on. Today the next download or
  sync of a target takes its failed files again.
- `flnd check [--fix]` will compare the history with the disk: files missing, unknown, or of the
  wrong size; `--fix` repairs what it can.

## `flnd login`

Log in to a source, or with no source list every source's login: logged in or not, where the
login came from, and the login cookies that expire soon.

```sh
flnd login [source] [how]
```

`how` is one of:

- **a browser**: `chrome`, `firefox`, `edge`, `brave` or `chromium`. flnd reads the site's
  cookies from it and keeps only those of the site's domains;
- **a cookie file's path**: a Netscape `cookies.txt` or a JSON export;
- **the source's own way**: Telegram (`tlg`) logs in by `browser` (Telegram Web's session),
  `qr` or `phone`, which ask their questions in the terminal.

Left out in a terminal, `how` is asked with numbered choices. Off a terminal, or with `--json`,
leaving it out is a usage error. A login is checked before it is saved: the cookies must hold
one of the source's login cookies. It is stored encrypted in the home.

```sh
flnd login
flnd login ins chrome
flnd login twt ~/Downloads/cookies.txt
flnd login tlg qr
```

## `flnd logout`

Remove a source's saved login.

```sh
flnd logout ins
```

## `flnd sources`

List the sources: their names, the targets they read, their categories and their ways to log in.
With `--json`, one object per source.

## What a run prints

- **`--ui plain`** writes one line per file on standard output: `saved <path>`, or
  `failed <target> <post>: <message>` with the hint on the next line; with `--dry`,
  `new <path>` or `saved <path>`. With `-v`, a skipped file gets a line too.
- **`--ui inline`** draws a live view on standard error: one row per target and category, with
  its files saved, skipped and failed, its size and whether it is still listing. Failures print
  above it as they happen. The rows stay on the screen at the end.
- **`--json`** writes each event as one JSON object per line, its class in `"type"` (`Saved`,
  `Skipped`, `Failed`, `Progress`, `Planned`), then the run's report as `{"type": "Report", …}`.
  `ls`, `login`, `logout`, `unfollow` and `sources` write their rows the same way.

The end of a run prints a summary: `done: 3 files saved (1.2 MB), 2 already saved`, and on
standard error how many failed and of which kind. flnd's own warnings (a wait a site asked for, a
login about to expire) go to standard error too.

## Exit codes

| code | meaning |
| --- | --- |
| 0 | everything worked |
| 1 | some files or targets failed (each is named above the exit); flnd went on with the rest |
| 2 | a usage error: an unknown command or flag, a target no source reads, a bad `--only`, `--when` or `--limit` |
| 3 | a login is needed: a source isn't logged in, its login was refused, or `flnd login` failed |
| 4 | flnd itself stopped: a full or refused disk, a missing tool, a broken source, or a bug |
| 130 | stopped with Ctrl-C |

A cron job can tell "some files failed" (1) from "flnd broke" (4). Each error class has its
code; [errors.md](errors.md) lists them.

A mistake you can fix (usage, login, disk, a tool) prints `error: <what went wrong>`, then what
to do on the next line, and never a traceback. A bug prints one line naming a crash file,
`<home>/logs/crash-<time>.txt`, which holds the traceback; with `-v` the traceback prints
instead.

## Ctrl-C

- The **first** Ctrl-C stops listing and lets the files in progress finish.
- The **second** stops at once. Unfinished files stay as parts (`<name>.part`), which the next
  run resumes, and it resumes each category from its saved cursor.

## For contributors

`make try ARGS="<target> [--only …] [--when …]"` runs `flnd --home .try download --dry` on the
target: it lists, live, the files a download would save, and downloads and records nothing. The
scratch home `.try` holds no login at first; log in there for a site that needs one
(`uv run flnd --home .try login ins chrome`). `make clean` removes it.

`make manifest` regenerates `flnd/sources/_manifest.py`, the sources as plain data that
`--help` and target resolution read; run it after changing a source's domains, routes or
categories (`tests/test_manifest.py` fails until then).
