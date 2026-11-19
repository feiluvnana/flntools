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
uv run flnd get ins:example_user/posts
```

`flnd --help` fits in 24 lines: a row per command or group, the flags, and the code of every
built-in plugin. `flnd get --help` adds a row per built-in plugin: the targets it reads, then
its categories. The help imports no plugin, so it answers in a few tens of milliseconds.

```text
  flnd 0.2.0 · download whole accounts from the web

  get [target…]                   Get what the targets have
  target list|clean               List the targets flnd knows, or forget some
  auth list|login|logout          List the accounts, log one in or out
  plugin list|install|uninstall   List the plugins, install or uninstall one
  extension install|uninstall     Connect Chrome to flnd, or remove it
  status                          Show the last run
  config                          Work in every setting of config.toml

  -h, --help                      show this help message and exit
  -V, --version                   print flnd's version
  --home DIR                      where downloads, history and logins live
                                  (default: $FLND_HOME, else ./.flnd)
  -v, --verbose                   show flnd's debug lines, and a bug's traceback
  --json                          print events and rows as JSON lines and never
                                  asks

  plugins                         tik fbk ins twt loi tlg mht web
                                  a plugin's targets: flnd get -h
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
- **Tables** (`flnd target list`, `flnd auth list`, `flnd plugin list`, `flnd config`) have
  plain columns two spaces apart on a pipe.
- Text from a site (a file name, a caption) never drives the terminal: its escape and control
  codes are dropped before it is printed.

## Global flags

These go before or after the command: `flnd --home ~/Media/flnd get` and
`flnd get --home ~/Media/flnd` are the same.

- `--home DIR` (default: `$FLND_HOME`, else `./.flnd`; what it does: the home: downloads, history
  and logins ([storage.md](storage.md)))
- `-v`, `--verbose` (default: off; what it does: show flnd's debug lines too, and a bug's traceback
  instead of a crash file)
- `--json` (default: off; what it does: print events and rows as JSON lines, for scripts and cron,
  and never ask a question)
- `--help`, `-h` (what it does: print the help of flnd, or of the command it follows)
- `--version`, `-V` (what it does: print flnd's version)


A flag given on the command line wins; then the environment (`FLND_HOME`); then the home's
`config.toml` ([storage.md](storage.md#settings-configtoml)); then the default.

## Targets

A target is `<plugin>:<name, id or link>`, `<plugin>?<words>` for a search, or a bare link.
Its categories go after its last `/`: `ins:example_user/posts,reels`, or `/all` for every one.
Without them, a target the home knows takes the categories it was followed with, and a new one
its site's own content; a search passes its categories on to each target it finds. A link
takes the defaults, and a single post has no categories, so naming one for it is a usage error:

```sh
flnd get ins:example_user
flnd get ins:example_user/posts,reels
flnd get "mht?example words/chapters"
flnd get https://www.tiktok.com/@example_user/video/1234567890
```

`flnd get --help` shows the forms each plugin accepts in one row, and `flnd plugin list` lists
each plugin. The pages under [plugins/](README.md#sites) explain them site by site.

## `flnd get`

Get what the targets have, and follow them, so `flnd get` alone keeps them up to date.

```sh
flnd get [targets…] [--when WHEN] [--limit N] [--dry] [--again] [--exec CMD]
```

A target that isn't a single post is added to the followed ones, with the categories it names.
A file already saved is skipped, so getting a target again fetches only what is new. A file
whose bytes another target already saved becomes a hard link to that file, so it takes no more
space ([storage.md](storage.md#dedupe)).

- `<targets…>` (default: standard input; what it does: the targets; when standard input isn't a
  terminal, its lines are added, skipping blank lines and lines starting with `#`. With none from
  either, every followed target is synced, or `nothing to sync` is printed and flnd exits 0)
- `--when WHEN` (default: `when` under `[defaults]` in `config.toml`, else every day; what it does:
  only posts from these days: a year `2026`, a month `2026-06`, a day `2026-06-15`, a range
  `2026-01..2026-03` (either end may be left out), or a span back from today `30d`, `8w`, `1y`;
  `--when ""` takes every day, whatever the config says)
- `--limit N` (default: no limit; what it does: at most N posts per target, or N targets found by a
  search; at least 1)
- `--dry` (default: off; what it does: list what would be saved (each file `new` or `saved`, or the
  targets a search finds), and save and write nothing)
- `--again` (default: off; what it does: list every post again, past the stop at saved posts and the
  saved cursors, and ask again for files the site answered 404 or 410 for, which are otherwise
  recorded gone and skipped ([storage.md](storage.md)))
- `--exec CMD` (default: none; what it does: run a command once for each saved file, without a
  shell: see [Running a command per file](#running-a-command-per-file))


Archives (`.zip`, `.7z`, `.rar`, `.tar*`) are always extracted beside themselves, into a folder
of their name; the archive file is kept too. 7z and RAR need 7-Zip (`7zz`) on your PATH.

```sh
flnd get ins:example_user
flnd get ins:example_user/reels --when 2026-01..2026-06
flnd get tik:example_user/videos twt:example_user/media
flnd get "mht?example/chapters" --limit 5 --dry
cat targets.txt | flnd get
flnd get
```

A followed search gets only the targets it hadn't found before. Every `get` of a target first
retries that target's failed files, on at most 3 runs per file ([errors.md](errors.md#retrying)).

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
flnd get ins:example_user --exec 'cp {path} /Volumes/backup/'
flnd get --exec 'notify-me --title {target} {path}'
flnd get --exec 'sh -c "echo \"$FLND_PATH\" >> saved.txt"'
```

A placeholder is filled inside the argument it is in, and the value is never read again, so a
name holding `;`, `$(…)` or `{post}` stays plain text. To use a shell's features, call the shell
yourself, as the last example does, and read the values from the environment, never by pasting
`{path}` into the shell's script. CMD's program must be on your PATH (or a full path), and a
mistake in CMD (unclosed quotes, no such program) is a usage error before anything is
saved. A command that exits non-zero prints a line on standard error and the run goes on.
With `--json`, the command's output goes to standard error, so standard output stays JSON. On
Windows, backslashes in CMD are kept as they are, for paths, and only double quotes group words.

## `flnd target`

```sh
flnd target list
flnd target clean <targets…> [--media]
```

- `list` works in the targets this home knows, followed or not: their files and size, when they
  last ran, and their categories. On a terminal it is a list: `↑↓` move, `/` filters, `enter`
  opens a detail panel, `f` follows or unfollows the target under the cursor, `r` makes its
  failed files retryable, `a` asks for a target and gets it, `d` forgets one after a
  confirmation, `q` quits. On a pipe it is a plain table; with `--json`, one
  `{"type": "Tracked", …}` object per target.
- `clean` forgets the targets: their rows, files rows, failures, cursors, heads and gaps. A
  target the home doesn't know is a usage error. `--media` also deletes their recorded files,
  sidecars, links, extracted archive folders and the folders this leaves empty. Files under
  `media/` that no row names are never deleted. A terminal is asked first ("Forget 2 targets and
  delete 1.2 GB?"); a pipe, and `--json`, aren't.

## `flnd auth`

```sh
flnd auth list
flnd auth login <plugin> [method] [path]
flnd auth logout <plugin> [account]
```

- `list` works in every account of every plugin: its name, whether it is the plugin's default,
  where its login came from, and its status (`ok`, `held 4 min`, `expires in 2 days`,
  `expired`). On a terminal the list has the keys above, and `*` makes the account under the
  cursor its plugin's default, `a` logs one in, `d` logs it out. On a pipe it is a plain table.
- `login` logs an account in. `method` is one of the plugin's login methods:
  - **`browser`**: flnd's Chromium opens, visible, at the site's login page; log in there, and
    the window closes once the login cookie is set (it waits up to 10 minutes);
  - **`chrome`**: the site's cookies from your Chrome, read through the extension;
  - **`file`**, with the path of a Netscape `cookies.txt` or a JSON export (a path given alone
    means `file`);
  - **`firefox`**: the site's cookies from your Firefox;
  - **`safari`**, on macOS only: the site's cookies from Safari. It needs Full Disk Access for
    the terminal, in System Settings › Privacy & Security; the error says so when it is missing;
  - **a plugin's own methods**: Telegram (`tlg`) also has `qr` and `phone`, which ask their
    questions in the terminal. Its `browser` opens Telegram Web's session.

  flnd keeps only the cookies of the site's domains from any of them.

  Left out in a terminal, `method` is asked. Off a terminal, or with `--json`, leaving it out is
  a usage error. A login is checked before it is saved: the cookies must hold one of the
  plugin's login cookies. It is sealed in the home's vault, `metadata/secret.db`, under a key
  kept outside the home ([storage.md](storage.md#the-vault)). Each account the site names is
  kept apart; logging in again as one replaces its login.
- `logout` logs one account out, or the plugin's default when none is named; a terminal is asked
  first, a pipe isn't.

On a terminal, the questions are forms in the style of Huh, drawn inline: a bar `┃` and a
title, `↑↓` choose, `enter` confirms, `esc` goes back and Ctrl-C stops. Logging in, for example:

```text
  ┃ Log in to Instagram
  ┃
  ┃ ❯ In a browser window      flnd opens Chromium; log in as usual
  ┃   From your Chrome         through the extension
  ┃   From a cookie file       a cookies.txt you exported
```

Without a terminal, a question that has no answer (the method of a login) is a usage error that
names what was needed; a yes-or-no question isn't asked, and the command goes on.

```sh
flnd auth list
flnd auth login ins chrome
flnd auth login twt file ~/Downloads/cookies.txt
flnd auth login tlg qr
flnd auth logout ins example_user
```

## `flnd plugin`

```sh
flnd plugin list
flnd plugin install <file|folder>
flnd plugin uninstall <code>
```

- `list` works in the plugins: the built-ins and the installed ones, with the kind
  (`built-in`, `installed`, `disabled`), the plugin API, its domains and how many targets use it.
  A plugin that is disabled is followed by its reason, naming its file and line. On a terminal
  the list has the keys above, and `a` installs the plugin at a path, `d` uninstalls an
  installed one (a built-in has none). With `--json`, one `{"type": "Installed", …}` object per
  plugin.
- `install` checks a plugin's `.py` file or package folder, copies it into the plugin folder
  and records it; an installed code is replaced.
- `uninstall` removes an installed plugin, after asking a terminal. Its accounts and targets
  stay.

Plugins load from `$FLND_PLUGINS`, else `~/.config/flnd/plugins/` (`%APPDATA%\flnd\plugins\` on
Windows); never from a home ([writing-a-plugin.md](writing-a-plugin.md#10-shipping-it-as-a-plugin)).
A file or folder in that folder that `install` didn't record is ignored, with a warning that
names `flnd plugin install`. `flnd plugin list` imports every installed plugin; `flnd --help`
imports none.

## `flnd extension`

```sh
flnd extension install
flnd extension uninstall
```

The extension lets flnd read this browser's cookies and pages; [extension.md](extension.md)
explains it. `install` copies the extension to `~/.config/flnd/extension/unpacked/` (its secret
stays one folder up, outside the extension), writes its native-messaging host for every Chromium
browser found (Chrome, Chromium, Edge, Brave), and prints three steps: open
`chrome://extensions`, turn on Developer mode, and click Load unpacked on that folder. It then
waits up to 10 minutes for the extension to connect. The popup acts on the home given here
(`--home`, else `$FLND_HOME`, else `./.flnd`). `uninstall` removes the folder and the hosts.

`flnd host` is hidden from the help: Chrome starts it, with the extension's origin (and, on
Windows, `--parent-window`), and it refuses any other origin.

## `flnd status`

Show the last run that downloaded, its failures, the accounts that need care, and the disk
against the history:

```sh
flnd status
flnd status --json
```

- The **last run**: its command, times and what it saved, then its failures grouped by target
  (10 shown, then "and N more"), each with its message, how many runs failed it, and what to
  do. A file a later run saved is left out.
- The **accounts** that are held, expired or expiring within a week.
- The **disk**: files missing, unknown, or of the wrong size
  ([storage.md](storage.md#checking-a-home)).

On a terminal with rows to repair (files missing or of the wrong size) it asks "Repair 2
rows? (y/N)", and a yes makes the history match the disk (`fl.check(fix=True)`); unknown files
are only reported. It exits 0 when all is well, and 1 when a failure, an account or the disk
needs care. With
`--json`, it prints one `{"type": "Status", "status", "accounts", "disk"}` object.

## `flnd config`

Work in the settings of `config.toml`: each key, its value, its default and where the value comes
from (`home`, `user` or `default`). On a terminal it is a list: `e` edits the value under the
cursor in a form, `a` asks for another key (a plugin's `plugins.ins.pace`) and its value, and `d`
resets a key to its default. The file is edited with tomlkit, so its comments stay, and a value
the key doesn't take is a message in the list. On a pipe it is a plain table; `--json` prints
one `{"type": "Setting", …}` object per key. The keys are in
[storage.md](storage.md#settings-configtoml).

## What a run prints

- **On a terminal**, a run draws its screen on standard error, from the run's snapshot
  (`run.state`, [library.md](library.md#what-a-run-is-doing-now)). It is redrawn only when the
  snapshot changed, at most 30 times a second, so a run with nothing going on writes nothing:

  ```text
    flnd get · 3 targets · 01:42

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
  `Skipped`, `Failed`, `Planned`, then the run's report as `{"type": "Report", …}`). The lists
  and `status` write their rows the same way. An error that ends a command is a line too,
  `{"type": "Error", "error": {"class", "message", "hint"}}`, besides its text on standard
  error. So every line on standard output is one JSON object with a `type`. A target in an
  event or a row is `{"text": "ins:example_user", "kind": "user"}`: its text as written, and
  the route that reads it.

The end of a run prints a summary on standard output, then each failure on standard error,
at most 10, with its message and its hint:

```text
✗ done in 4m 12s · 3 targets · 1,021 saved (3.3 GB) · 1,220 already saved · 1 failed

✗ twt:example_creator · media/1290_2.mp4
  the site answered 404: the file is gone
  hint  nothing to do; flnd won't ask for it again

details  flnd status
```

It starts with `✓` when nothing failed. Nothing prints after it. Each run's events also go to
`<home>/log/<time>_get.jsonl`, one JSON object per line.

flnd's own warnings (a wait a site asked for, a login about to expire) go to standard error too,
and the repeats of a warning within 10 seconds fold into one line ([How it looks](#how-it-looks)).

## Exit codes

- 0: everything worked
- 1: some files or targets failed (each is named above the exit); flnd went on with the rest. `flnd
  status` exits 1 when something needs care
- 2: a usage error: an unknown command or flag, a target no plugin reads, a category its route
  lacks, a bad `--when` or `--limit`
- 3: a login is needed: a plugin isn't logged in, its login was refused, or `flnd auth login` failed
- 4: flnd itself stopped: a full or refused disk, a missing tool, a broken plugin, or a bug
- 130: stopped with Ctrl-C


A cron job can tell "some files failed" (1) from "flnd broke" (4). Each error class has its
code; [errors.md](errors.md) lists them.

A mistake you can fix (usage, login, disk, a tool) prints `✗ <what went wrong>`, then
`hint  <what to do>` on the next line, and never a traceback (`x` instead of `✗` off UTF-8):

```
✗ ins:example_user needs a login
  hint  flnd auth login ins
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

`make try ARGS="<target>[/categories]"` runs `fl.get(<target>, dry=True)` in the scratch home
`.try`: it lists, live, the files a get would save, and saves and records nothing. The scratch
home holds no login at first; log in there for a site that needs one
(`uv run flnd --home .try auth login ins chrome`). `make clean` removes it.

`make manifest` regenerates `src/plugins/_manifest.py`, the built-in plugins as plain data that
`--help` and target resolution read; run it after changing a plugin's domains, routes or
categories (`tests/test_manifest.py` fails until then).

The code is `src/cli.py`: `parser()` holds every command and flag, and `main` returns the exit
code. Rules a change must keep:

- Import nothing heavy at the top: argparse, os and sys only. Everything else is imported by
  the command that needs it, so `flnd --help` pays for none of it.
- One flag, one spelling: no aliases. This page names every command and flag, and
  `tests/test_docs.py` checks it against the parser.
- `--json` never asks a question: a command that would ask is a usage error instead. Every
  line it prints on standard output is one JSON object with a `type`, an error included.
- `--exec` never starts a shell: CMD is split once with `shlex`, and a value from a site (a
  file name, a post id) only ever fills a placeholder inside one argument.
