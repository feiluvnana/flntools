# The command line

Every command and flag `flnd` has, with its default and an example. `flnd --help`, and `--help`
after any command, print the same from the code: each command's help is its function's
docstring in `src/cli.py`.

The CLI is a thin layer over the library ([library.md](library.md)): each command makes one
library call, and a run's events become lines on the screen. Anything the command line does, a
script can do too.

## Running it

From a checkout, `make setup` creates the environment, then:

```sh
uv run flnd --help
uv run flnd download ins:example_user/posts
```

`flnd --help` fits in 24 lines: a row per command, the flags, and the code of every built-in
plugin. `flnd download --help` and `flnd follow --help` add a row per built-in plugin: the targets
it reads, then its categories. The help imports no plugin, so it answers in a few tens of
milliseconds.

```text
  flnd 0.1.0 · download whole accounts from the web

  download [target…]     Download what the targets have now
  follow [target…]       Follow targets
  …
  sources                tik fbk ins twt loi tlg mht
                         their targets: flnd download -h
```

## How it looks

Every line flnd prints is drawn from one theme (`src/ui/theme.py`), through `src/ui/out.py`:

- **Colours.** Body text is light grey; hints, borders and zeros are dim grey; titles and
  commands are blue (`focus`); what worked is green, what failed is red, and warnings are
  yellow.
- **No colour** when `NO_COLOR` is set, when `TERM` is `dumb`, or when the output goes to a
  pipe or a file: the text is the same, without escape codes.
- **No Unicode** on a terminal that isn't UTF-8: `✓ ✗ • · ❯` become `v x * - >`, and the spinner
  is `|/-\`.
- **Warnings fold.** A warning is shown at once, and opens a window of 10 seconds. Its repeats
  inside the window are held. When the window has passed (at the next warning), before a run's
  end summary, or when the command ends, a warning that repeated shows once more as
  `text (×n)`, n counting every time it came in the window, the first included: one repeat
  shows `text (×2)`.
- **Errors** print their message, then what to do on the next line (see
  [Exit codes](#exit-codes)).
- **Tables** (`flnd ls`, `flnd login`) have a rounded border on a terminal, and are plain columns
  two spaces apart on a pipe.
- Text from a site (a file name, a caption) never drives the terminal: its escape and control
  codes are dropped before it is printed.

## Global flags

These go before or after the command: `flnd --home ~/Media/flnd sync` and
`flnd sync --home ~/Media/flnd` are the same.

| flag | default | what it does |
| --- | --- | --- |
| `--home DIR` | `$FLND_HOME`, else `./.flnd` | the home: downloads, history and logins ([storage.md](storage.md)) |
| `-v`, `--verbose` | off | show flnd's debug lines too, and a bug's traceback instead of a crash file |
| `--json` | off | print events and rows as JSON lines, for scripts and cron, and never asks a question |
| `--help`, `-h` | | print the help of flnd, or of the command it follows |
| `--version`, `-V` | | print flnd's version |

A flag given on the command line wins; then the environment (`FLND_HOME`); then the home's
`config.toml` ([storage.md](storage.md#settings-configtoml)); then the default.

## Targets

A target is `<plugin>:<name, id or link>`, `<plugin>?<words>` for a search, or a bare link.
Its categories go after its last `/`: `ins:example_user/posts,reels`, or `/all` for every one.
Without them, a target the home knows takes the categories it was followed with, and a new one
its site's own content; a search passes its categories on to each target it finds. A link
takes the defaults, and a single post has no categories, so naming one for it is a usage error:

```sh
flnd download ins:example_user
flnd download ins:example_user/posts,reels
flnd download "mht?example words/chapters"
flnd download https://www.tiktok.com/@example_user/video/1234567890
```

`flnd download --help` shows the forms each plugin accepts in one row, and `flnd sources` lists
each plugin with its first forms. The pages under [plugins/](README.md#sites) explain them site
by site.

## `flnd download`

Download what the targets have now, once. Nothing is followed: a later `flnd sync` doesn't take
these targets (`flnd follow` does).

```sh
flnd download <targets…> [--when WHEN] [--limit N] [--dry] [--again] [--exec CMD]
```

| flag | default | what it does |
| --- | --- | --- |
| `<targets…>` | standard input | the targets; with none, flnd reads them from standard input, one per line, skipping blank lines and lines starting with `#` |
| `--when WHEN` | `when` under `[defaults]` in `config.toml`, else every day | only posts from these days: a year `2026`, a month `2026-06`, a day `2026-06-15`, a range `2026-01..2026-03` (either end may be left out), or a span back from today `30d`, `8w`, `1y`; `--when ""` takes every day, whatever the config says |
| `--limit N` | no limit | at most N posts per target, or N targets found by a search; at least 1 |
| `--dry` | off | list what would be downloaded (each file `new` or `saved`, or the targets a search finds), and download and write nothing |
| `--again` | off | list every post again, past the stop at saved posts and the saved cursors, and ask again for files the site answered 404 or 410 for, which are otherwise recorded gone and skipped ([storage.md](storage.md)); `again = true` under `[defaults]` in the home's `config.toml` makes it the default |
| `--exec CMD` | none | run a command once for each saved file, after every steps chain, without a shell: see [Running a command per file](#running-a-command-per-file) |

Archives (`.zip`, `.7z`, `.rar`, `.tar*`) are always extracted beside themselves, into a folder
of their name; the archive file is kept too. 7z and RAR need 7-Zip (`7zz`) on your PATH.

Examples:

```sh
flnd download ins:example_user
flnd download ins:example_user/reels --when 2026-01..2026-06
flnd download tik:example_user/videos twt:example_user/media
flnd download "mht?example/chapters" --limit 5 --dry
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
| `{path}` | `FLND_PATH` | the file's final path: on disk |
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
flnd follow <targets…> [--when WHEN] [--again]
```

The categories a target names are kept with it for later syncs; following it again with others
replaces them. `--when` bounds this first download only. The flags read as `download`'s do.

## `flnd sync`

Download what is new for every followed target, or for the ones named.

```sh
flnd sync [targets…] [--exec CMD]
```

Each target keeps the categories it was followed with. A sync lists each category newest first and

stops it after a few saved posts in a row, so a sync with nothing new costs a few requests per
target. A followed search downloads only the targets it hadn't found before.

A sync has no `--when`, but
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
- an **unknown** file under `media/` is only reported: a later run that lists its post finds
  it on disk, skips it, and records its row.

[storage.md](storage.md#checking-a-home) says what check passes over.
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

Log in to a plugin's site, or with no source list every plugin's login: logged in or not, where
the login came from, and the login cookies that expire soon.

```sh
flnd login [source] [how]
```

`how` is one of:

- **a browser**: `chrome`, `firefox`, `edge`, `brave`, `chromium`, or `safari` on macOS
  (which needs Full Disk Access for the terminal, in System Settings › Privacy & Security; the
  error says so when it is missing). flnd reads the site's
  cookies from it and keeps only those of the site's domains;
- **a cookie file's path**: a Netscape `cookies.txt` or a JSON export;
- **the plugin's own way**: Telegram (`tlg`) logs in by `browser` (Telegram Web's session),
  `qr` or `phone`, which ask their questions in the terminal.

Left out in a terminal, `how` is asked with numbered choices. Off a terminal, or with `--json`,
leaving it out is a usage error. A login is checked before it is saved: the cookies must hold
one of the plugin's login cookies. It is stored in the home's vault, `metadata/secret.db`, a
file only your account can read ([storage.md](storage.md)).

```sh
flnd login
flnd login ins chrome
flnd login twt ~/Downloads/cookies.txt
flnd login tlg qr
```

## `flnd logout`

Remove a plugin's saved login.

```sh
flnd logout ins
```

## `flnd sources`

List every plugin: the built-ins, the installed ones, and where each came from (`built-in`,
`package` for an installed package's entry point, `file` for the plugin folder), its version,
the targets
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
([writing-a-plugin.md](writing-a-plugin.md#10-shipping-it-as-a-plugin)). This command imports
every one of them; `flnd --help` imports none. `flnd login`, `flnd ls` and every download know
the installed plugins as they know the built-ins. With `--json`, one object per plugin, with
`"type": "Source"` and the fields of `flnd.Installed`.

## `flnd extension`

Install flnd's Chrome extension, or uninstall it. The extension lets flnd read this browser's
cookies and pages; [extension.md](extension.md) explains it.

```sh
flnd extension install
flnd extension uninstall
```

`install` copies the extension to `~/.config/flnd/extension/unpacked/` (its secret stays one
folder up, outside the extension), writes its native-messaging host
for every Chromium browser found (Chrome, Chromium, Edge, Brave), and prints three steps: open
`chrome://extensions`, turn on Developer mode, and click Load unpacked on that folder. It then
waits up to 10 minutes for the extension to connect. The popup acts on the home given here
(`--home`, else `$FLND_HOME`, else `./.flnd`). `uninstall` removes the folder and the hosts.

`flnd host` is hidden from the help: Chrome starts it, with the extension's origin (and, on
Windows, `--parent-window`), and it refuses any other origin.

## What a run prints

- **On a terminal**, a run draws its screen on standard error, from the run's snapshot
  (`run.state`, [library.md](library.md#what-a-run-is-doing-now)). It is redrawn only when the
  snapshot changed, at most 10 times a second, so a run with nothing going on writes nothing:

  ```text
    flnd download · 3 targets · 01:42

    target               categories        saved  skipped  failed      size     speed
    ✓ ins:example_user   posts reels         412    1,208       0    2.1 GB         –
    ⠹ twt:example_creator media               37       12       1  180.0 MB 31.0 MB/s
    · tik:example_handle videos                –        –       –         –         –

    ⠹ media/1290_2.mp4          ━━━━━━━━━━━━━━━━━━╺━━━━━━━━━━━  63%   38.0 MB 12.0 MB/s
      + 4 more

    now  twt:example_creator · media, page 4 · held 30 s: 429 from api.example.com (×3)

    42.0 MB/s · 449 saved · 1 failed                       ctrl+c stop · twice: stop now
  ```

  One row per target: its glyph (`·` waiting, the spinner while it lists or downloads, `✓`
  done, `✗` failed), the target and its categories (cut in the middle to 28 and 14 cells), its
  counts (`–` before it counted anything), its size and speed. Then up to 6 files in flight,
  their paths cut at the start to 24 cells, with `+ <n> more` for the rest; queued files never
  show. The `now` line says what the run does, in yellow while a site makes it wait. On a
  terminal too short for every row, the last targets fold into `+ <n> targets`. A warning
  prints above the block.
- **On a pipe**, nothing is drawn while the run goes; the summary comes at the end.
- **With `--dry`**, standard output lists what the run would save: `found <target>` per target
  a search finds, then `new <path>` or `saved <path>` per file, and a last line counts them.
- **`--json`** writes each event as one JSON object per line, its class in `"type"` (`Saved`,
  `Skipped`, `Failed`, `Planned`, then the run's report as `{"type": "Report", …}`.
  `ls`, `login`, `logout`, `unfollow`, `sources` and `check` write their rows the same way. An
  error that ends a command is a line too, `{"type": "Error", "error": {"class", "message",
  "hint"}}`, besides its text on standard error. So every line on standard output is one JSON
  object with a `type`. A target in an event or a row is `{"text": "ins:example_user",
  "kind": "user"}`: its text as written, and the route that reads it.

The end of a run prints a summary on standard output, then each failure on standard error,
at most 10, with its message and its hint:

```text
✗ done in 4m 12s · 3 targets · 1,021 saved (3.3 GB) · 1,220 already saved · 1 failed

✗ twt:example_creator · media/1290_2.mp4
  the site answered 404: the file is gone
  hint  nothing to do; flnd won't ask for it again

details  flnd status
```

It starts with `✓` when nothing failed. Nothing prints after it.

flnd's own warnings (a wait a site asked for, a login about to expire) go to standard error too,
and the repeats of a warning within 10 seconds fold into one line ([How it looks](#how-it-looks)).

## Exit codes

| code | meaning |
| --- | --- |
| 0 | everything worked |
| 1 | some files or targets failed (each is named above the exit); flnd went on with the rest |
| 2 | a usage error: an unknown command or flag, a target no plugin reads, a category its route lacks, a bad `--when` or `--limit` |
| 3 | a login is needed: a plugin isn't logged in, its login was refused, or `flnd login` failed |
| 4 | flnd itself stopped: a full or refused disk, a missing tool, a broken plugin, or a bug |
| 130 | stopped with Ctrl-C |

A cron job can tell "some files failed" (1) from "flnd broke" (4). Each error class has its
code; [errors.md](errors.md) lists them.

A mistake you can fix (usage, login, disk, a tool) prints `✗ <what went wrong>`, then
`hint  <what to do>` on the next line, and never a traceback (`x` instead of `✗` off UTF-8):

```
✗ ins:example_user needs a login
  hint  flnd login ins
```

A mistake in the flags is one too:

```
✗ flnd: unrecognized arguments: --nope
  hint  flnd -h lists what it takes
```

A bug prints `✗ flnd hit a bug: <the error>`, then
`hint  details in <home>/log/crash-<time>.txt`, the file that holds the traceback; with `-v`
the traceback prints instead.

## Ctrl-C

- The **first** Ctrl-C stops listing and lets the files in progress finish.
- The **second** stops at once. Unfinished files stay as parts (`<name>.part`), which the next
  run resumes, and it resumes each category from its saved cursor; a sync cut short this way is
  finished by the next sync, which jumps to where it stopped
  ([architecture.md](architecture.md#smart-sync)). It says `Ctrl-C again to quit now`, since
  closing the browser and writing the last rows may take a moment.
- The **third** quits at once, exit code 130: it first runs the undo steps anything registered
  for the terminal, then exits without waiting for anything. The parts on disk stay and resume
  as after a second Ctrl-C; the rows of the posts in progress are lost, and the next run finds
  their files on disk and skips them. Warnings still folded are dropped, without their
  `(×n)` line.

## For contributors

`make try ARGS="<target>[/categories] [--when …]"` runs `flnd --home .try download --dry` on the
target: it lists, live, the files a download would save, and downloads and records nothing. The
scratch home `.try` holds no login at first; log in there for a site that needs one
(`uv run flnd --home .try login ins chrome`). `make clean` removes it.

`make manifest` regenerates `src/plugins/_manifest.py`, the built-in plugins as plain data that
`--help` and target resolution read; run it after changing a plugin's domains, routes or
categories (`tests/test_manifest.py` fails until then).
