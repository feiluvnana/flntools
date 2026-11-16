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
| `--ui full\|inline\|plain` | `full` on a terminal, else `plain`; `ui` under `[defaults]` in the home's `config.toml` changes it | how a run shows: the full screen, a live view under the prompt, or one line per file ([What a run prints](#what-a-run-prints)) |
| `-v`, `--verbose` | off | show flnd's debug lines too, and a bug's traceback instead of a crash file |
| `--json` | off | print events and rows as JSON lines, for scripts and cron; implies `--ui plain`, and never asks a question |
| `--help`, `-h` | | print the help of flnd, or of the command it follows |
| `--version`, `-V` | | print flnd's version |

A flag given on the command line wins; then the environment (`FLND_HOME`); then the home's
`config.toml` ([storage.md](storage.md#settings-configtoml)); then the default.

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
flnd download <targets…> [--only CATS] [--when WHEN] [--limit N] [--compress] [--dry] [--again] [--exec CMD]
```

| flag | default | what it does |
| --- | --- | --- |
| `<targets…>` | standard input | the targets; with none, flnd reads them from standard input, one per line, skipping blank lines and lines starting with `#` |
| `--only CATS` | every category | only these categories, such as `reels,posts`; may be given more than once. With targets from several sources, `ins:reels` names one source's. A single post has no categories, so `--only` with one is a usage error |
| `--when WHEN` | `when` under `[defaults]` in `config.toml`, else every day | only posts from these days: a year `2026`, a month `2026-06`, a day `2026-06-15`, a range `2026-01..2026-03` (either end may be left out), or a span back from today `30d`, `8w`, `1y`; `--when ""` takes every day, whatever the config says |
| `--limit N` | no limit | at most N posts per target, or N targets found by a search; at least 1 |
| `--compress`, `--no-compress` | `compress` under `[defaults]` in `config.toml`, else off | replace each saved photo and video by a smaller copy that looks the same, when one passes every check, and remove each archive once it is extracted ([media.md](media.md)); `--no-compress` turns a configured default off for this run |
| `--dry` | off | list what would be downloaded (each file `new` or `saved`, or the targets a search finds), and download and write nothing |
| `--again` | off | list every post again, past the stop at saved posts and the saved cursors, and ask again for files the site answered 404 or 410 for, which are otherwise recorded gone and skipped ([storage.md](storage.md)); `again = true` under `[defaults]` in the home's `config.toml` makes it the default |
| `--exec CMD` | none | run a command once for each saved file, after every step (compress included), without a shell: see [Running a command per file](#running-a-command-per-file) |

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

A file already saved is skipped, so downloading a target again fetches only what is new. A
file whose bytes another target already saved becomes a hard link to that file, so it takes no
more space ([storage.md](storage.md#dedupe)).

### Running a command per file

`--exec CMD` runs CMD for each saved file, as the run goes, one at a time and in the order the
files finish. It never starts a shell: CMD is split into arguments once, the way a POSIX shell
splits words (`shlex`), and then run directly. A file name comes from a site, so it can't be
trusted; it only ever fills a placeholder inside one argument, and is never read as a command.

| placeholder | environment | what it becomes |
| --- | --- | --- |
| `{path}` | `FLND_PATH` | the file's final path: after `--compress`, the compressed file's |
| `{target}` | `FLND_TARGET` | the target it was saved for, `ins:example_user` |
| `{post}` | `FLND_POST` | the post's id, as the site gives it |

```sh
flnd download ins:example_user --exec 'cp {path} /Volumes/backup/'
flnd sync --exec 'notify-me --title {target} {path}'
flnd sync --exec 'sh -c "echo \"$FLND_PATH\" >> saved.txt"'
```

A placeholder is filled inside the argument it is in, and the value is never read again, so a
name holding `;`, `$(…)` or `{post}` stays plain text. To use a shell's features, call the shell
yourself, as the last example does, and read the values from the environment, never by pasting
`{path}` into the shell's script. CMD's program must be on your PATH (or a full path), and a
mistake in CMD (unclosed quotes, no such program) is a usage error before anything is
downloaded. A command that exits non-zero prints a line on standard error and the run goes on.
With `--json`, the command's output goes to standard error, so standard output stays JSON. On
Windows, backslashes in CMD are kept as they are, for paths, and only double quotes group words.

## `flnd follow`

Follow targets, so `flnd sync` keeps them up to date, and download what they have now.

```sh
flnd follow <targets…> [--only CATS] [--when WHEN] [--compress] [--again]
```

`--only` and `--compress` are kept with each target for later syncs (a target followed with
`--compress`, or with `compress = true` in the config, has its new files compressed by every
sync); following again replaces them. `--when` bounds this first download only. The flags read
as `download`'s do.

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
without being opened ([media.md](media.md)). Without the flag, `compress` under `[defaults]` in
the home's `config.toml` decides, and `--no-compress` overrides it. A sync has no `--when`, but
`when` under `[defaults]` bounds it too.

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

## `flnd check`

Compare the history with the disk: files missing, unknown, or of the wrong size.

```sh
flnd check [--fix]
```

It reads every row of `flnd.db` and walks the home's `media/` folder once; nothing is fetched.
Each problem prints one line (`missing`, `unknown` or `wrong size`, then the path), and a last
line counts them. It exits 0 when history and disk agree, and 1 when they don't.

`--fix` makes the history match the disk. It never deletes, moves or downloads a file:

- a **missing** file's row is removed, so `flnd download <target> --again` can fetch it again;
- a file of the **wrong size** gets its row's size and hash from the disk;
- an **unknown** file under `media/` is adopted into the history, so a later run skips it. A
  crash between a file's rename and its row leaves such a file: if the run recorded a failure
  for that path, the file takes that failure's key, target and post.

[storage.md](storage.md#checking-a-home) says what check passes over and how a file is adopted.
With `--json`, it prints one `{"type": "Checked", ...}` object.

## `flnd status`, `flnd retry`

The end of a run that left failures prints one line on standard error, by kind:

```
✗ 3 files failed: 2 network (retry: flnd retry), 1 gone. Details: flnd status
```

- `flnd status` shows the last run that downloaded: its command and times, what it saved, and
  each failure it left, with its message, how many runs failed it, and what to do. A file a
  later run saved is left out. With `--json`, it prints one `{"type": "Status", ...}` object,
  its `failures` a list of `{target, post, path, error: {class, message, hint}, attempts, url,
  at}`. It exits 0.
- `flnd retry` downloads again every file that failed, however many runs failed it: each failed
  post is listed again for fresh links, from the page it was on, and only its failed files are
  downloaded. A target whose listing failed is downloaded again. A gone file (404 or 410) isn't
  retried; `download --again` asks for those. It exits like a download.

Every `download` and `sync` also retries a target's failed files by itself first, on at most 3
runs per file ([errors.md](errors.md#retrying)).

## `flnd login`

Log in to a source, or with no source list every source's login: logged in or not, where the
login came from, and the login cookies that expire soon.

```sh
flnd login [source] [how]
```

`how` is one of:

- **a browser**: `chrome`, `firefox`, `edge`, `brave`, `chromium`, or `safari` on macOS
  (which needs Full Disk Access for the terminal, in System Settings › Privacy & Security; the
  error says so when it is missing). flnd reads the site's
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

List every source: the built-ins, the plugins, and where each came from (`built-in`, `package`
for an installed package's entry point, `file` for the plugin folder), its version, the targets
it reads, its categories, and its login state with its ways to log in. A plugin that is
disabled is listed with the reason, naming its file and line:

```sh
flnd sources
```

```text
ins  Instagram  (built-in 0.1.0)
  targets:    ins:me, ins:<user>, https://www.instagram.com/p/<code>/, …
  categories: posts, reels, tagged, reposts, stories, highlights, profile
  login:      logged in (browser, file)
exm  Example  (file 1.0)
  from:       /home/example_user/.config/flnd/sources/exm.py
  targets:    exm:<user>, https://www.example.com/p/<id>/, https://www.example.com/<user>/
  categories: posts
  login:      needs no login
bad  bad  (file)
  disabled:   /home/example_user/.config/flnd/sources/bad.py, line 3: NameError: name 'Sorce' is not defined
```

Plugins load from `$FLND_SOURCES`, else `~/.config/flnd/sources/` (`%APPDATA%\flnd\sources\`
on Windows), and from installed packages; never from a home
([writing-a-source.md](writing-a-source.md#10-shipping-it-as-a-plugin)). This command imports
every one of them; `flnd --help` imports none. `flnd login`, `flnd ls` and every download know
the plugin sources as they know the built-ins. With `--json`, one object per source, with
`"type": "Source"` and the fields of `flnd.Installed`.

## What a run prints

- **`--ui full`**, the default on a terminal, takes the whole terminal (its alternate screen)
  while the run goes, redrawn 10 times a second:
  - a header: the run, its state (running, paused, stopping), the time since it started, and
    the totals: files saved, already saved and failed, their bytes, the speed, and the time
    left once every listing is done;
  - **Targets**: one row per target and category, the busy ones first: listing, finishing (the
    listing is done, files are still downloading), done, caught up (a sync met 5 saved posts in
    a row), cut (`--limit`), stopped or failed; with the pages its paged listing read, posts seen, files over
    found, and size. A wide terminal also shows how close a listing is to stopping early;
  - **Downloads**: each file in flight with its bar, size, speed and host (`↻` when it went on
    from a part an earlier run left), how many are queued, and the busiest hosts as files in
    flight over their adaptive cap, with `+N` waiting;
  - **System**: browser tabs open (and the most a target holds), the browser's and flnd's RAM,
    flnd's CPU, ffmpeg and Pillow jobs, files waiting for a compress or unpack step, and free
    disk on the home's volume;
  - **Log**: each wait first, with its reason (`www.example.com waits 2m 58s  Retry-After`,
    `back-off`, or the source's pace), then flnd's latest lines and failures.

  Keys: **`q`** stops gracefully, as a first Ctrl-C does; **`p`** pauses (no new request or file
  starts; files in flight finish) and resumes. The footer shows the Ctrl-C stages as they
  happen. On exit the terminal is put back as it was, and what scrollback should keep is
  printed on the normal screen: each failure with its hint (a dry run's plan on standard
  output), then the summary. It reads 80 × 24 and uses the room a bigger terminal gives. A
  question during a run (whether to download Chromium) gives the terminal back while it asks.
  `--exec`'s commands write to the terminal under the screen: use `--ui inline` to read their
  output.
- **`--ui plain`** writes one line per file on standard output: `saved <path>` (the file's final
  path, once every step after it is done), or
  `failed <target> <post>: <message>` with the hint on the next line; with `--dry`,
  `new <path>` or `saved <path>`. With `-v`, a skipped file gets a line too.
- **`--ui inline`** draws a live view on standard error: one row per target and category, with
  its files saved, skipped and failed, its size and whether it is still listing. Failures print
  above it as they happen. The rows stay on the screen at the end.
- **`--json`** writes each event as one JSON object per line, its class in `"type"` (`Saved`,
  `Skipped`, `Failed`, `Progress`, `Planned`, and a `Stats` snapshot about once a second; see
  [library.md](library.md#live-stats)), then the run's report as `{"type": "Report", …}`.
  `ls`, `login`, `logout`, `unfollow`, `sources` and `check` write their rows the same way. An
  error that ends a command is a line too, `{"type": "Error", "error": {"class", "message",
  "hint"}}`, besides its text on standard error. So every line on standard output is one JSON
  object with a `type`.

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

- The **first** Ctrl-C (or `q` on the full screen) stops listing and lets the files in
  progress finish.
- The **second** stops at once. Unfinished files stay as parts (`<name>.part`), which the next
  run resumes, and it resumes each category from its saved cursor; a sync cut short this way is
  finished by the next sync, which jumps to where it stopped
  ([architecture.md](architecture.md#smart-sync)). It says `Ctrl-C again to quit now`, since
  closing the browser and writing the last rows may take a moment.
- The **third** quits at once, exit code 130: the terminal's mode and the normal screen are put
  back first, then flnd exits without waiting for anything. The parts on disk stay and resume
  as after a second Ctrl-C; the rows of the posts in progress are lost, and the next run finds
  their files on disk and skips them.

## For contributors

`make try ARGS="<target> [--only …] [--when …]"` runs `flnd --home .try download --dry` on the
target: it lists, live, the files a download would save, and downloads and records nothing. The
scratch home `.try` holds no login at first; log in there for a site that needs one
(`uv run flnd --home .try login ins chrome`). `make clean` removes it.

`make manifest` regenerates `flnd/sources/_manifest.py`, the sources as plain data that
`--help` and target resolution read; run it after changing a source's domains, routes or
categories (`tests/test_manifest.py` fails until then).
