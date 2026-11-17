# PLAN: a lean flnd on a reusable flncore

Written 2026-11-17, from the state of `main` at 49ff2dd. This plan answers seven hints from the
user (quoted in each section) and turns them into ordered steps. Each step is one or a few
commits, keeps `make check` green, and runs `make bench ARGS=--gate` when it touches a hot
path. When the work is done, this file is deleted and what lasts moves into `flnd/docs/`.

## Where we start

Measured on 49ff2dd:

| what | size |
| --- | --- |
| `flnd/` (37 modules, 7 sources) | 37,400 lines |
| biggest modules | `job.py` 3,209 · `_net.py` 2,368 · `source.py` 2,239 · `_browser.py` 2,101 · `engine.py` 1,785 · `_home.py` 1,683 · `cli.py` 1,618 |
| tests | 22,100 lines |
| public surface (`tests/data/api.txt`) | 419 lines, 165 of them `flnd.source` |
| `Site` (what a source acts through) | 26 members |
| CLI commands | 11: `download follow sync unfollow ls status retry check login logout sources` |

What is wrong today:

1. **The live log drifts.** Old files' lines move or repeat once new files start, because the
   rows on screen and the rows in memory stop lining up. Suspects, each checked in step 4 (the drift test of step 0 reproduces them first):
   - `flnd/_ui.py` uses `TaskID(0)` to mean "not live", but Rich gives its first task the id
     `0` as well (checked: `Progress().add_task()` returns 0). A row keyed by 0 can be taken
     for another row.
   - Each file opens its own `ui.progress` group on one shared Rich `Progress`. Rows are drawn
     in the order they were added. When a row finishes, it is removed and printed above, so
     every row below it shifts up, and Rich redraws the region at once.
   - A label wider than the terminal wraps onto two lines. Rich's cursor math then counts
     one line too few, and the region creeps down the screen.
   - More live rows than the terminal is tall. Rich can't redraw rows that have scrolled off.
   - The console writes through `cli._RunLogStream`, which tees into `<home>/log/*.log`. Every
     redraw frame of the bars lands in the log file too, with the escape codes stripped out.
2. **A file has no one line that says everything.** Its bar, a later `saved …` line, and a
   `failed …` line with a hint are separate outputs, written from different places
   (`_transfer._Row`, `cli._Plain`, `_ui._unregister_group`).
3. **The CLI is wide.** It has 11 commands, and `download`, `follow` and `sync` overlap.
4. **General code lives inside flnd.** HTTP, the browser, files, cookies, the vault, the store,
   the log and prompts are already written as standalone libraries (`tests/test_layers.py`,
   rule 5), but they are `flnd._*` modules, so no other script can use them without
   importing flnd.
5. **Features flnd doesn't need:** `--compress` (`_shrink`, Pillow, pyoxipng, the compress
   lane, `Chain.backlog`, `make media-real`), the `ui` setting, which is left over from the
   removed full screen, and live stats (`_stats`, psutil, `Stats` events), which fed that
   screen.
6. **Sources do the engine's job.** There are 10 `site.back_off` calls in the built-in sources:
   each source paces itself when the site pushes back, so each one gets it slightly
   differently.
7. **`make check` is red in the working tree.** `examples/` was deleted there (uncommitted),
   so ruff fails with `E902 examples`, and 2 of the 1,299 tests fail
   (`test_plugins.ExamplesTest`). `HEAD` itself is green. **`examples/` goes for good** (user,
   2026-11-17), and step 0 finishes the removal. A runnable example is still kept, where it
   can't go stale:
   - the `python` blocks of `flnd/docs/library.md` and `flnd/docs/writing-a-source.md`, which
     `test_docs` runs;
   - `tests/example_source.py`, a whole source with its test.

Found in an audit, beyond the hints:

8. **Dead arguments break the one-spelling rule.** Five parameters are documented as "unused;
   kept for the callers": `job.py:358` (`slots`), `job.py:611` (`source`), `job.py:698`
   (`row`), `job.py:781` (`file`) and `_transfer.py:691` (`decode`). `_vault.py:223` keeps a
   "legacy key path, for compatibility". Each one goes, along with its callers' arguments.
9. **Modules reach into each other's privates.** `cli.py:1446` and `_transfer.py:576,620`
   call `ui._progress_enabled()`. The new `flncore.log` makes "is the screen live" a public
   read. A test then forbids `x._name` access across modules.
10. **`_prompt` guesses that it is in a test** (`_is_mock`: standard input is a `StringIO`)
    and swallows any questionary error with `except Exception: pass`, falling back to plain
    input. A broken prompt should say so. Whether to ask is decided once (`interactive`), and
    tests pass a fake terminal instead of the code sniffing for one. Its docstring also says
    "imports only the stdlib", but it imports questionary.
11. **The CLI duck-types its own library.** `cli.py` has 19 `getattr` and `hasattr` calls on
    objects whose types it knows (`getattr(fl, "home", None)`, `hasattr(self.out,
    "log_only")`). Each becomes a plain attribute access on a real type.
12. **`<home>/log/run.log` is overwritten by every run**, so two runs at once clobber each
    other's log. The stamped `<stamp>_<command>.jsonl` is enough, and `_home.sweep` already ages
    old logs out. `run.log` goes.
13. **61 broad `except Exception` blocks and 25 `except …: pass`.** Some are right, such as an
    undo step during the third Ctrl-C. Step 12 checks each one: it either names the errors it
    expects, or logs at debug with a comment saying why it is safe.
14. **Commit hygiene.** 49ff2dd mixed a DASH fix, a CLI rewrite (812 lines) and the removal of
    `_screen.py`. From here on it is one logical change per commit, as AGENTS.md says.

## Target shape

```
flntools/
├── flncore/              a kit for any program that fetches things from the web
│   │   how it looks
│   ├── log.py            log lines + one live line per item (Charm Huh look); no TUI
│   ├── prompt.py         questions (Charm Huh look)
│   ├── cli.py            cyclopts + a Charm Huh help formatter, error/hint printing, JSON lines
│   │   how it runs
│   ├── run.py            work on its own thread, read as events; 2-stage Ctrl-C; the hold watchdog
│   ├── loop.py           one asyncio loop on its own thread, called from sync code (`run`, `submit`)
│   ├── errors.py         Error (hint, retryable); SiteError with Blocked, NotFound, Changed; Stopped
│   ├── clock.py          now, sleep, and a fake for tests; every wait goes through it
│   │   how it fetches
│   ├── net.py            curl_cffi + retry policy, pacing, the AIMD cap, resumable downloads
│   ├── browser.py        one Playwright browser on its own loop: tabs, feeds, human-like scroll
│   ├── media.py          ffmpeg: merge a DASH video with its audio, with a deadline
│   │   how it saves
│   ├── files.py          parts that lock themselves, hashed as written; place, names, kinds, unpack
│   ├── dig.py            read parsed JSON by a path (the fastest way measured)
│   ├── config.py         a TOML file into a typed struct, with errors that name the key; set/remove
│   └── testing.py        fakes: HTTP, clock, terminal, outside tools
├── flnd/                 the downloader: what only flnd needs
│   ├── __init__.py       Flnd, events, errors (built on flncore.errors)
│   ├── source.py         source API 2 (re-exports dig, Blocked, NotFound)
│   ├── testing.py        the test kit for sources (built on flncore.testing)
│   ├── cli.py            the commands: annotated functions on flncore.cli
│   ├── _library.py       Flnd and open: the library API
│   ├── _events.py        the events and rows, all tagged msgspec structs
│   ├── _run.py           flnd's Run: flncore's Run plus the Report
│   ├── engine.py  job.py  listing.py  _transfer.py  site.py  home.py  options.py  plugins.py
│   ├── _html.py          selectolax parse, and Meta's blocks and meta() (fbk, ins)
│   ├── _secret.py        cookie files and the secret store: plaintext, mode 0600, private
│   ├── _extension.py     the extension's native host and protocol, private
│   └── sources/
├── extension/            the flnd Chrome extension: MV3, plain JS, no build step
└── tests/  flnd/docs/
```

Rules that change (AGENTS.md and `flnd/docs/architecture.md` are updated in the same steps):
- **Two packages, one wheel.** `flncore` and `flnd` ship together (`[tool.hatch...] packages =
  ["flncore", "flnd"]`), and PyInstaller collects both. `core` imports nothing from `flnd`
  (`tests/test_layers.py` gets a new rule 0). A source still imports only `flnd.source`.
- **Public surface:** `flnd`, `flnd.source` and `flnd.testing` keep their API snapshot.
  `flncore` gets a snapshot of its own (`tests/data/flncore-api.txt`), so a change to it is
  reviewed the same way.
- **What goes in `flncore`, by one test:** a module goes there when it adds something its
  dependency doesn't (a policy, a safety rule, a hot path) *and* a program other than flnd
  would want it, such as a scraper, a backup script or another downloader:

  | module | in `flncore`? | why |
  | --- | --- | --- |
  | `log`, `prompt`, `cli` | yes | the look, built by flnd itself |
  | `run`, `errors` | yes | a run read as events and stopped by Ctrl-C; one error type with a hint |
  | `clock` | yes | `net`'s retries and `browser`'s waits sleep through it, so tests can fake time |
  | `loop` | yes | One asyncio loop on a thread that sync code calls into, carrying contextvars (the fake clock) and turning Ctrl-C into a cancel; `submit` never waits for the loop |
  | `net` | yes | When to retry (429 with `Retry-After`, 5xx, safe methods only), how hard to press a host (the AIMD cap), and resuming into a part with mirrors. curl_cffi gives none of it |
  | `browser` | yes | Driving Playwright from its own loop thread, response feeds in order, human-like scrolling, finding Chrome (or offering Chromium) |
  | `media` | yes | Merging DASH video and audio through ffmpeg while both still stream into their parts (flushed as written, 49ff2dd). That flag set and the ordering are easy to get wrong |
  | `files` | yes | Self-locking parts, hashing as bytes are written (the hot path), atomic placing with a date, names safe on every OS, archives unpacked without path traversal |
  | `dig` | yes | Reading JSON fast. JSONPath was measured 4× to 40× slower |
  | `config` | yes | A TOML file into a typed struct, with an error that names the bad key and says what it takes |
  | `testing` | yes | The fakes the modules above need: HTTP, clock, terminal, tools |
  | cookie files, the secret store (`_secret`), the extension's host | no | Cookie files and a 0600 store are small. The extension and its host are flnd's own, with flnd's name and permissions |
  | the SQLite store | no | `sqlite3` with three pragmas, and its schema is flnd's |
  | HTML (`parse`, Meta's `blocks`) | no | `parse` is selectolax itself; `blocks` reads Meta's pages only |
  | plugins, engine, job, listing, home, source API | no | what flnd is |

  `flncore` imports no `flnd`, and its modules import each other only downward, in this
  order: look → run → fetch → save. `tests/test_layers.py` checks both rules, and that a
  source imports only `flnd.source`.
- Imports stay lazy where it is free (a command imports what it uses), but the 40 ms
  `--help` budget is dropped (user, 2026-11-17), along with `tests/test_packaging.py`'s
  timing check.

## The flncore API

`flncore` follows this repo's own conventions (AGENTS.md, API and naming), and is held to them
the same way. `tests/test_names.py` and `tests/test_docs.py` cover it, and
`tests/data/flncore-api.txt` is its snapshot. In short:
- short names, one word where one word says it;
- an action is a verb, and a read is a property;
- options are keyword-only, at most 5;
- a resource is a context manager;
- one spelling, with no aliases;
- every error is a `flncore.Error` with a `hint`;
- every public name has a signpost docstring and a section in `flnd/docs/flncore.md`, whose examples the docs test runs.

Everything is imported from the top, each name loaded on first use:

```python
from flncore import log, ask, confirm, choose, App, Run, Error
```

**`log`: lines and items.** It is one shared object, and writes to standard error.

```python
log.info("saved {path}", path=path)      # also debug, ok, warn, error; fields are sanitised
log.level = "warn"                       # debug, info, ok, warn, error, quiet

with log.item("ins:example_user/reels/a.mp4", total=size) as item:   # one line, live
    item.advance(len(chunk))             # moves the bar; any thread
    item.note("merging")                 # the text after the bar
# leaving the block ends it ok; an exception ends it failed, with the error's text
item.skip("already saved")               # or end it yourself: ok(note), skip(note), fail(note)

with log.file(home / "log" / "20261117-091203_get.jsonl") as journal:   # JSON Lines
    journal.write(event)                 # any msgspec struct or dict, with "at" and "type" added
```

`log.file` records every log line as a `Log` object by itself. Items aren't recorded, since they
are display: a program writes its own outcome records with `journal.write`, as flnd does with
its events. One encoder (msgspec) serves every machine-readable output.

- `Item` has `ok`, `skip`, `fail`, `advance`, `note` and the reads `state` (the `State` enum),
  `count` and `total`. That is the whole of it: `ok` and `fail` mirror `log.ok` and
  `log.error`.
- `log.scope(level=, dest=, color=, width=)` swaps the console inside a block, for tests and
  `--color`. It is the one place the destination changes.
- Today's `ui.group`, `ui.progress`, `Group`, `Row`, `drawn=` and `_progress_enabled` are
  gone. `log.live` (a property: True while lines are being redrawn) answers what
  `_progress_enabled()` was called for.

**`ask`, `confirm`, `choose`: questions.** Off a terminal, each returns its default, or
raises `UsageError` when it has none.

```python
name = ask("Name", default="example_user")
code = ask("Code", secret=True)
if confirm("Download Chromium now", default=False): ...
how = choose("Log in with", {"cookies from chrome": "chrome", "a cookie file": "file"})
```

**`App`: a command line from annotated functions** (cyclopts underneath, with our help
formatter):

```python
app = App("flnd", version=__version__, codes={UsageError: 2, LoginError: 3})
target = app.group("target", "the targets this home knows")

@app.command
def get(*targets: str, all: bool = False) -> int:
    """Download what the targets have now.

    Args:
        targets: `ins:example_user`, `ins:example_user/reels,posts`, a link, or `mht?words`.
        all: every category, other people's content included.
    """

@target.command
def follow(*targets: str) -> None:
    """Have `flnd sync` keep these up to date."""

raise SystemExit(app.run())    # parse, run, print an Error and its hint, return the exit code
```

- A command's help is its docstring, and its flags are its parameters. `-v` and `-h` are the
  app's own: `-v` sets `log.level` to debug and shows a bug's traceback, and `-h` prints the
  help with the version on its first line. Without `-v`, a bug's traceback goes to a crash file.
- `app.show(rows, columns)` prints rows as an aligned table in the Charm Huh look, which
  `target list`, `auth list` and `plugin list` use.
- `App` takes 3 options, plus `command`, `group`, `run` and `show`.

**`Run`: work on a thread, read as events.**

```python
run = Run(work, name="get")      # work(emit, stop) runs on its own thread on the first read
for event in run:                # Ctrl-C once: stop.soon; twice: quit, exit code 130
    ...
result = run.wait()              # what work returned; it re-raises what work raised
```

`run.stop()` asks it to stop. flnd's own `Run` subclasses this one to add `report`.

**`Error`: the base error.**
- It takes `Error(message, *, hint="", retryable=False)`, its text is `str(e)`, and its
  advice is `e.hint`. It has no `target`: what an error was about lives on the event.
- Its subclasses are `UsageError`, `NetworkError`, `DiskError`, `ToolError`, and
  `SiteError(status=None)`, the site refused or answered wrongly, by HTTP or in a page. Under
  `SiteError` come `Blocked(wait=)`, `NotFound` and `Changed`.
- `Stopped` derives from `KeyboardInterrupt`, not from `Error`.
- `Error.of(exception)` turns any exception into one of them, without changing the
  exception it was given. It replaces `classify`, as a constructor that reads as a noun.
  `DiskError.of(error, path)` replaces `disk`.

flnd's own errors (`LoginError`, `SourceError`) subclass these, so `flnd.Error` is
`flncore.Error`, and there is still one hierarchy.

## The CLI

> "flnd get [targets] --all · flnd sync · flnd target list/syncon/syncoff/forget ·
> flnd auth login/logout · flnd status · flnd plugin list/add/remove"
> "avoid options on flnd … only -v and --all and -h are enough" (user, 2026-11-17)

```text
flnd get <targets…> [--all]          download what the targets have now (targets also from stdin)
flnd sync [targets…]                 what is new for every followed target, or the ones named
flnd target list [targets…]          the targets this home knows: followed or not, files, size, last run
flnd target follow <targets…>        sync keeps these up to date
flnd target unfollow <targets…>      sync leaves these out; files and history stay
flnd target remove <targets…>        flnd forgets them (files on disk stay); a later get starts from the top
flnd auth list                       every source's login, and where it came from
flnd auth login <source> [how]       how: extension (the default), a cookie file, or the source's own (qr, phone)
flnd auth logout <source>
flnd status                          the last run, its failures, the logins, and the disk against the history
flnd plugin list
flnd plugin install <file.py | folder>
flnd plugin remove <code>
flnd extension install               set up the flnd Chrome extension, and wait until it connects
flnd extension remove                undo what install did
flnd config init                     write <home>/config.toml with every key, commented; again adds new keys
flnd config set <key> <value>        e.g. `dedupe off`, `sidecars on`, `ins.proxy socks5://127.0.0.1:1080`
flnd config remove <key>             back to the default

flags: -v (debug lines, and a bug's traceback) · --all (get only) · -h (help, with the version)
```

**`flnd -h` fits on one screen** (user, 2026-11-17: "flnd -h is also verbose"). Today it is
126 lines: 11 commands in boxes, then every target form of every site (13 for Facebook
alone). It becomes:

```
flnd 0.2.0 · download what you follow, and keep it up to date

  get <targets…> [--all]                   download what the targets have now
  sync [targets…]                          download what is new for the followed targets
  target     list · follow · unfollow · remove
  auth       list · login · logout
  plugin     list · install · remove
  extension  install · remove
  config     init · set · remove
  status                                   the last run, its failures, the logins, the disk

targets  ins:example_user · ins:example_user/reels · a link · mht?words
sites    ins tik twt fbk tlg loi mht   (flnd get -h: each site's categories)
flags    -v debug lines and tracebacks · -h help
docs     flnd/docs/start.md
```

- **The rules for help text:** one line per command, taken from its docstring's summary
  line, with no boxes and no wrapped descriptions.
- **`flnd <group> -h`** shows that group's commands, one line each.
- **`flnd get -h`** adds one line per site: its code, name and categories, with its own
  categories marked. That's 7 lines, not 90.
- **Each site's target forms and link shapes** live in `flnd/docs/sources/<code>.md`. A
  target that no site reads is a `UsageError` whose hint names the site that owns the
  domain and its page.
- **The Charm Huh look stays:** the palette, bold commands and dim descriptions, drawn by
  `flncore.cli`'s help formatter. A test pins the output and its length, at no more than 24
  lines.

**Three flags, and nothing else.** Each old option goes to the place it belongs:

| old option | where it goes | why |
| --- | --- | --- |
| `--only reels,posts` | **into the target:** `ins:example_user/reels,posts`. A tab's link reads the same way: `https://www.instagram.com/example_user/tagged/` is `ins:example_user/tagged` | A category belongs to one target, not to the command. `--only tik:videos --only twt:media` was a sign of that. No site allows `/` in a user name, so the form is unambiguous, and an unknown category is a `UsageError` naming the ones that exist |
| `--when`, `--limit`, `--dry` | the library: `fl.get(..., when=, limit=, dry=)` | They are for scripts and for trying a source out. `make try` becomes a short script (`tests/try.py`) on `dry=True` |
| `--again` | `flnd target remove <target>`, then `get` | Removing drops what flnd keeps about the target: its row, heads, cursors, gaps, failures (gone files included) and file rows. The next `get` lists everything again. A file still on disk is found by its path and recorded again from one `stat`, never downloaded, so only the missing ones download. One way, not two |
| `--json` | the `.jsonl` run log (see The log), and the library | Machines read the log file or call the library. Standard output is for people |
| `--home DIR` | `$FLND_HOME`, else `./.flnd`, as today | A home is a setting of where you run, not of one command |
| `-V`, `--version` | the first line of `-h` | |
| `status --check --fix` | `flnd status` always checks the disk (one walk of `media/`) and reports. Repair is `fl.check(fix=True)` in the library, or `target remove` then `get` for a target with missing files | |

A bare `flnd` prints the help. `get` and `sync` show a live line per file in flight, and leave
one line per target and one per failure (see The log). They exit with the codes that
`flnd/docs/cli.md` lists, which are unchanged.

Where the old commands go:

| old | new |
| --- | --- |
| `download` | `get` |
| `follow` | `get` then `target follow`. `get --sync` is left out on purpose, so there is one way to do it |
| `unfollow` | `target unfollow` |
| `ls` | `target list` |
| `retry` | removed: every `get` and `sync` already retries failed files first. `status` names the files that ran out of tries, and `flnd get <target>` retries them whatever their count (decision 15) |
| `check` | `status` |
| `login`, `logout` | `auth login`, `auth logout`; `auth list` lists the logins |
| `sources` | `plugin list` (the built-ins are listed as `built-in`) |

**`--all`:** without it, `get` takes a target's *own* categories (posts, reels/videos,
stories, highlights, profile). `--all` adds the rest (tagged, reposts, liked, replies'
parents). A source marks each category `own=True|False`. Categories named in the target
override both. This brings back the 2026-11-11 default, which 2026-11-12 removed. gallery-dl
does the same (its Instagram `include` defaults to posts). It also stops a first `get` of a
big account from quietly pulling in thousands of other people's files. A followed target
keeps the categories it was followed with.

**`remove` is the one verb for "flnd stops keeping this":** `target remove`, `plugin remove`
and `extension remove` (user, 2026-11-17), with `install` as its pair for what is put on
the machine. `remove` deletes only what flnd itself made: a target's rows, a plugin's copy
in the plugin folder, the extension's folder and host manifests. It never deletes a media
file. `fl.add(cls)` stays a different word on purpose: it adds a source to this
process only, and copies nothing.

**`follow`/`unfollow`, not `syncon`/`syncoff`:** "syncon" isn't a word, and the naming rules
want verbs. `follow` is already the word in the library, the docs and the database
(`targets.sync`). It is also the one people know from the sites themselves.

## The library

The library is where the options went, so it is the full API. A script never needs the CLI.

```python
import flnd

with flnd.open() as fl:                                   # $FLND_HOME, else ./.flnd
    for event in fl.get("ins:example_user/reels"):        # a Run; reading it runs it
        if isinstance(event, flnd.Saved):
            print(event.path)

    run = fl.get(["tik:example_user", "twt:example_user"], all=True, when="2026", limit=50)
    report = run.wait()                                   # flnd.Report: saved, skipped, failed …

    fl.sync()                                             # every followed target

    # One collection per command group, with the group's verbs:
    fl.targets.follow("ins:example_user")                 # flnd target follow; also unfollow, remove
    fl.auth.login("ins")                                  # flnd auth login; also logout
    fl.plugins.install("~/exm.py")                        # flnd plugin install; also remove
    fl.extension.install()                                # flnd extension install; also remove
    fl.config.set("dedupe", "off")                         # flnd config set; also init, remove
    for row in fl.targets: ...                            # iterating a collection lists it

    fl.status                                             # a read is a property
    fl.files(target="ins:example_user", kind="video")     # a read with filters is a method
    fl.check(fix=True)                                    # flnd.Checked
    fl.add(MySource)                                      # a source for this process only
```

The rules it follows:
- **`get` has 4 keyword options:** `all`, `when`, `limit` and `dry`. `sync` has none.
- **The library mirrors the CLI's groups.** `fl.targets`, `fl.auth`, `fl.plugins`,
  `fl.extension` and `fl.config` are collections with exactly the verbs of `flnd target`,
  `flnd auth`, `flnd plugin`, `flnd extension` and `flnd config`. Iterating one gives its rows
  (`Tracked`, `Login`, `Installed`, and for `config` each `Setting` with its value, default
  and description), and `len()` and `in` work. `config` has no `list` command, but a script
  can still read the settings this way. The top level keeps what belongs to no group:
  `get`, `sync`, `status`, `files`, `check` and `add`.
- **`Flnd(home, *, again=)` loses `again`;** `targets.remove` replaces it.
- **Every action returns something useful.** `get` and `sync` return a `Run`. `follow`,
  `unfollow` and `remove` return the targets they changed, `auth.login` the `Login`,
  `auth.logout` whether there was one, and `plugins.install` the `Installed`.
- **A target is a string or a `Target`, and many are a list**, everywhere.
- **The events are `Saved`, `Skipped`, `Failed`, `Progress`, `Planned`, and `Report` at the
  end.** Each is a frozen msgspec struct, and `flnd.encode(event)` gives the JSON the run log
  holds.
- **Errors are `flnd.Error` with a `hint`**, as in AGENTS.md.

## The log

> "Each file should also have 1 log line to show all of its status … ui should be log only,
> tui is very error prone … charm huh style (keep that code also)"

**Quiet by default** (user, 2026-11-17: "flnd also prints so much"). A file has one live line
while it is in flight, and that line shows its whole status. When the file ends, its line
goes away. What stays in the scrollback is one line per target, and one line per failure,
since that is what a person acts on:

```
✓ ins:example_user   41 saved (512 MB) · 1,200 already saved
✗ tik:example_user   12 saved · 1 failed
    reels/2026-06-12_C8abe.mp4  network: timed out — retried on the next run
⠹ twt:example_user/media/2026-06-15_1803.mp4  ━━━━━━━━────────  4.1/9.8 MB   (live)
```

- **What `-v` adds:** a fixed line for every file (saved, skipped or failed), and the debug
  lines.
- **What is printed after the targets:** a summary line, only when there was more than one
  target: `done: 3 targets, 53 saved (640 MB), 1 failed`.
- **Warnings:** a line each (`! ins: waiting 60 s, the site asked to slow down`).
- **Off a terminal** (cron, a pipe): the same target lines, failures, warnings and summary,
  with nothing live and nothing per file. A cron mail stays short.
- **The full record of every file** is in the `.jsonl` run log, below.

How it is drawn (`flncore.log`), built so drift can't happen:
1. **Items have stable keys.** `log.item(key, label)` returns a handle. Its state is an
   `Enum` (`queued, active, merging, unpacking, done, skipped, failed`), plus a count, a
   total and a note. A key is never reused, so there is no sentinel id and no Rich task id.
2. **One renderer owns the screen.** It redraws at most 10 times a second, on one thread, from
   a snapshot of the items taken under one lock. No other code writes to the terminal while
   it is live: `log.info(...)` lines and finished items go into a queue, and the renderer
   prints them above the live region.
3. **Lines never wrap.** Every line is cut to the terminal width (the middle of the label is
   elided, as `…`), so one item is always one screen line and the cursor math is exact. The
   width is read from the real terminal on each frame, so a resize is handled too.
4. **The live region is capped** at `min(active items, terminal height − 2)`. Items past the
   cap fold into a single line, `… and 14 more`. A finished item leaves the region in one
   frame, together with the target line or failure line it adds, if any, so nothing shifts
   between frames.
5. **Off a terminal** (a pipe, cron), nothing is live, and items print nothing unless they
   fail. `log` lines print at once.
6. **The run log file is for machines** (user, 2026-11-17): `<home>/log/<stamp>_<command>.jsonl`,
   JSON Lines, one object per line, written as each thing ends, never as live frames:

   ```json
   {"at":"…","type":"Saved","target":"ins:example_user","post":"C8xYz","path":"media/ins/example_user/reels/C8xYz.mp4","size":10276044}
   {"at":"…","type":"Failed","target":"ins:example_user","post":"C8abe","error":{"class":"NetworkError","hint":"retried on the next run"}}
   ```

   The file opens with a `Start` line (command, version) and ends with the `Report`. The
   fields shown are a few of each event's; `flnd/docs/storage.md` lists them all.

   - **One format, not two.** Each event object is exactly what `flnd.encode(event)` gives, from the
     same `msgspec` encoder (`flnd._events.encode`), with `at` added. `Log` lines (`log.warn`
     and the rest) are objects too: their `message` is the formatted text, and their
     `fields` are the raw values, so a tool can filter by `source` without parsing English.
   - `Progress` events are left out of the file. They are many, and they repeat what
     `Saved`, `Skipped` and `Failed` already say. Debug lines are in the file only with `-v`.
   - Every line is written whole and flushed. A crash leaves at most a cut-off last line,
     which a reader skips. A file name or caption from a site is a JSON string, so its escape
     codes can't reach a terminal that `cat`s the file.
   - `flnd status` reads the last run from `flnd.db`, as today, not from this file: the file
     is for people's tools (`jq`, a dashboard), and it is never parsed by flnd itself.
   - A crash keeps its own `crash-<time>.txt` with the traceback, since a traceback is for a
     person, and the `.jsonl` file names it in a last `{"type":"Crash","file":…}` line.
   - `_RunLogStream` and the tee of the terminal go away. `_home.sweep` ages `.jsonl` files
     out as it does today.
7. **The look stays Charm Huh:** the current palette, glyphs and help panels (`PALETTE`,
   `GLYPHS`, `SPIN_FRAMES`, `CHARM_STYLE`, `RichArgumentParser`) move into `flncore.log`,
   `flncore.prompt` and `flncore.cli` unchanged.

**The file's state comes from the engine, once.** Today a file's state is spread across
`_transfer._Row`, the job's `Board`, the step chain and `cli._Plain`. After this change, the
job keeps one record per file whose state moves only forward, and both the events (`Saved`,
`Skipped`, `Failed`) and the log line are made from that record. The CLI turns events into
log items and makes no decisions of its own. A script reading events sees the same states.

## The flnd extension

> "Have a chrome extension named flnd. This extension will become one way to get the cookies
> and do advanced things to the browser that current flnd can't do." (user, 2026-11-17)

**Why it is worth building:**
- **Cookies, read the way the browser means them to be read.** The extension asks Chrome for
  a site's cookies (`chrome.cookies`, HttpOnly included) from the very browser that is logged
  in. It needs no keychain decryption and no Full Disk Access on macOS. It also keeps working
  under the app-bound cookie encryption that Chrome uses on Windows (since Chrome 127), which
  stops any program from reading the cookie database from outside. It replaces `_cookies.py`
  (1,126 lines) and the yt-dlp dependency, and logins stay fresh because the browser itself
  refreshes them.
- **A real browser.** Driving the user's own Chrome means its real fingerprint: no
  `navigator.webdriver`, no automation switches, and the user's own history and logins. A site
  that walls Playwright sees an ordinary visitor. This replaces the patchright trial.
- **Use from the page.** A toolbar button on a profile shows the target flnd reads there
  (resolved from the manifest's domains and forms), with **Get** and **Follow**, and the run's
  progress from its `.jsonl` log.

**How it works:**
1. **Two pieces.**
   - `extension/` at the repo root: Manifest V3 in plain JavaScript, with no build step.
     It has `manifest.json`, `background.js` (the service worker), `page.js` (a content
     script in the page's main world, which hands the responses it sees to the worker) and
     `popup.html`/`popup.js`.
   - `flnd/_extension.py`: the native-messaging host and the protocol.
2. **Installing: `flnd extension install`** (user, 2026-11-17: "maybe flnd has a command to
   install the extension"). One command does every step a program is allowed to do:
   1. It copies the extension's files from the package to a fixed folder:
      `~/.config/flnd/extension/`, or `%APPDATA%\flnd\extension\` on Windows. Chrome then
      keeps finding them after an upgrade of flnd. Running install again copies the new
      version, and the extension reloads itself when its files change.
   2. It writes the native host's manifest (`com.flnd.host`) for every Chromium browser it
      finds (Chrome, Edge, Brave, Chromium): into each browser's `NativeMessagingHosts`
      folder on macOS and Linux, or the registry under `HKCU` on Windows. The manifest allows
      only the extension's fixed ID, which the `key` in `manifest.json` pins.
   3. **The one step a person has to do.** Chrome refuses to load an unpacked extension that
      a program hands it: `--load-extension` was removed from branded Chrome in version 137,
      and `chrome://` pages can't be opened from outside. So install opens
      `chrome://extensions` where the OS allows it, copies the folder's path to the
      clipboard, and prints three numbered lines: turn on Developer mode, click
      **Load unpacked**, paste the path.
   4. It waits, with one live line, until the extension connects. Then it prints `✓ the flnd
      extension is connected (Chrome 131, extension 0.2.0)` and exits 0. Ctrl-C leaves
      everything installed so far, and running install again carries on.

   Once flnd is published on the Chrome Web Store (a later step, under the owner's account),
   step 3 becomes "click Add to Chrome" on the page install opens, and the unpacked path stays
   for development. `flnd extension remove` deletes the host manifests and the folder, and
   says how to remove the extension from Chrome, which only Chrome can do. `flnd status`
   shows whether the extension is installed and connected, and `auth login` without it says
   `run flnd extension install`.
   In the library: `fl.extension` is an `Extension` with `install()`, `remove()` and the
   reads `connected`, `version` and `folder`. The CLI's commands call these, as the rule
   "a script can do anything the CLI can" asks.
   **How it talks:**
   - The service worker opens the native port when Chrome starts. An open port keeps an MV3
     worker alive.
   - Chrome then runs `flnd host`, a command left out of the help, over standard input and
     output.
   - The host relays between the extension and running flnd processes over `<home>/host.sock`
     (a named pipe on Windows), which only the user can open.
3. **The protocol** is JSON messages: `{id, op, args}` in, `{id, ok, value | error}` out.
   - The ops are `cookies(domains)`, `open(url)`, `wait(tab, selector, timeout)`,
     `scroll(tab, …)`, `feed(tab, pattern)` (a stream of response bodies, in order),
     `document(tab)`, `click(tab, selector)` and `close(tab)`.
   - Each op is one `Site` browser verb, so a source runs the same on either backend. Its
     tabs open in a window of their own, which flnd closes at the end of the run.
4. **Which browser a run uses:** the extension's, when it is connected; otherwise flnd's own
   Chromium through Playwright, as today. A cron run on a server falls back that way. The
   log says which one at the start, in one line. A source whose site walls Playwright says
   `Browser(real=True)`, and without the extension it fails with the hint `flnd extension install`. The hint says how to
   install it.
5. **Logging in** becomes:
   - `flnd auth login ins`: through the extension, the default;
   - `flnd auth login ins cookies.txt`: a cookie file, for a server with no browser;
   - `flnd auth login tlg qr`: a source's own way.

   The `chrome`, `firefox`, `edge`, `brave`, `chromium` and `safari` hows go away, along with
   the Safari Full Disk Access advice.
6. **Security:**
   - Chrome lets only the extension talk to the host (`allowed_origins`). The socket is the
     user's alone (mode 0600, or a pipe with the user's ACL).
   - The extension's `host_permissions` are the built-in sources' domains, written from the
     manifest by `make manifest`. A plugin's domains are asked for at runtime
     (`optional_host_permissions`), so the user approves each new site in Chrome's own
     dialog.
   - Cookie values cross the pipe once, go straight into the vault, and are never logged:
     the protocol's debug lines are redacted.
   - The extension runs only its own files, as MV3 requires. So `Site.evaluate`, which no
     built-in source calls, leaves API 2: MV3 can't run code from a string.
7. **Tests.**
   - The protocol is unit-tested in `make check` against a fake extension, a Python peer on
     the socket.
   - The extension itself is tested by `make ext`. It loads the extension unpacked into
     Playwright's Chromium (`--load-extension`), against the local test site of
     `tests/bench/server.py`. It is never part of `make check`.
   - Live checks on real sites are by hand, as always.
8. **Other browsers:** the same code can run in Firefox later, since it is a WebExtension
   with native messaging too. Safari needs an app wrapper and is out. Cookie files cover
   everything else.

## Docs: flnd/docs

> "The docs will reflect every corner and how to use flnd, renamed to flnd/docs, while flnd
> comments will be kept to a few lines in each spot to guide the reader only." (user,
> 2026-11-17)

This replaces the rule of 2026-11-16 ("extensive comments and docs"). The explanation moves
out of the code and into one place, and the code keeps signposts.

**`flnd/docs/`** (renamed from `docs/`) is where every question about flnd is answered:

```
flnd/docs/
├── README.md            the map: which page answers what
├── start.md             install, the extension, the first get, the first sync, cron
├── cli.md               every command and argument, with examples and the exit codes
├── config.md            every key, flnd's and each source's, and init, set and remove
├── library.md           every public name of flnd, with examples that run
├── extension.md         installing it, what it reads, its permissions, when it doesn't connect
├── storage.md           the home's folders, flnd.db's tables, the .jsonl log's format, the secret store
├── sync.md              how sync works, for a user: heads, gaps, cursors, retried failures
├── errors.md            every error class: its exit code, its hint, and what to do
├── writing-a-source.md  every declaration, every Site member, Settings, and the test kit
├── flncore.md           the kit: each module, its API, with examples that run
├── architecture.md      for contributors: layers, threads, the state machine, the decisions
├── performance.md       the gates, how to read them, how to measure
├── glossary.md
└── sources/<code>.md    each site: its targets, its categories (own or not), its login, its
                         quirks with the date seen
```

`media.md` goes with compression; the DASH merge moves into `storage.md` and
`architecture.md`. `make docs` (pdoc) goes too: with short docstrings, the reference is the
pages.

**"Every corner" is a test, not a hope.** `tests/test_docs.py` fails when any of these is
missing from the page that owns it:
- every public name in the API snapshots (`flnd`, `flnd.source`, `flnd.testing`, `flncore`);
- every command and argument of the CLI;
- every config key, including each built-in source's `Settings`;
- every error class with its exit code;
- every event and row field;
- every `Site` member;
- every target form and category of each source, read from the manifest.

It also still runs every `python` block in the pages, and parses every `flnd` command line
in them. So a page that shows something wrong, or misses something new, fails `make check`.

**The code's comments are signposts** (the new "Writing" rules of AGENTS.md):
- **A module docstring** is 1 to 3 lines: what the module holds, and the `flnd/docs` page that
  explains it. There are no numbered flows, and no "who calls it" sections: those are in
  `architecture.md`.
- **A public class or function** has a summary line, then `Args:`, `Returns:` and `Raises:`,
  one line per item, only when they say more than the name and the type do. There is no
  `Example:`: examples live in `flnd/docs`, where they run.
- **A private function** gets a docstring only when its name doesn't say what it does.
- **A comment** is for what the code can't say: a site quirk with the date seen, a magic
  number's source, which thread a call runs on or which lock it needs, why a failure is
  safe to ignore. One or two lines, pointing to `flnd/docs` for more.
- **A change of behaviour** updates its `flnd/docs` page in the same commit, as before.

**What it does to the size:** docstrings and comments are about half of every file today. With
signposts instead, the estimates fall to about **11,000 lines for `flnd/` and `flncore/`**
(from 30,400) and **about 3,500 for the sources** (from 6,586). The measured numbers are
reported at each step.

## Custom code, and dependencies for the rest

> "flnd [is] trying to do everything, like job, net … can that be transferred to dependencies
> to simplify the current code. The only need to customize is cli (annotations), cli help,
> log, prompt (Charm style). Everything else tries to use a dependency to shorten the impl."

**The rule (it goes into AGENTS.md):** a dependency is taken only when it removes code that
is hard to get right, and when it keeps every behaviour and every bench gate. A dependency
that saves a few lines, changes what flnd does, or can't be driven by the fake clock, is
refused. It must have wheels for Python 3.11 to 3.14 on macOS, Linux and Windows. It is pinned
to a major version and has a contract test of the calls flnd makes.

**The verdict, after measuring and after the code review of 2026-11-17.** The first draft of
this plan swapped in nine libraries. The review read every line each one would have replaced,
and only one survived (tomlkit was added later, for `flnd config`):

| proposed | verdict | why (measured or read in the code) |
| --- | --- | --- |
| **cyclopts** for the CLI | **taken** | Commands become annotated functions, and their help is their docstring. It replaces argparse, `RichArgumentParser` and the hand-written help (about 400 lines). Startup cost (75 ms cold) is accepted now that the budget is gone |
| **tomlkit** for `flnd config set` / `remove` | **taken** | Editing TOML while keeping the user's comments and layout is the hard part, and the stdlib only reads TOML. Pure Python, no dependencies, 57 releases since 2018. It is imported only by those two commands; reading the file stays `tomllib` + msgspec |
| tenacity for retries | refused | flnd's policy has two budgets (429 rounds and other failures), a half-jitter wait, `Retry-After` and reset parsing, and hooks. tenacity would supply only the loop, saving about 10 lines for 12 µs more per request |
| aiolimiter for pacing | refused | Its timing runs on `loop.time()`, so the fake clock can't drive it, and it has no hold, no halve-on-429 and no stop event. The wrapper would be as big as `Bucket` (about 45 lines) |
| anyio's portal for the loop | refused | `start_task_soon` waits until the loop has scheduled the task, while `Loop.submit` is fire-and-forget. One task per file is submitted from the listing thread, so this risks `cpu.loop_task` (6.7 µs) and `e2e.latency_500_posts`. The 32.8 µs measured earlier was a raw `run_coroutine_threadsafe`, not `Loop` |
| keyring + Fernet for secrets | refused | A keychain that is locked over ssh or from cron would break unattended syncs. A key stored beside the data protects nothing. The secret store stays plaintext, as `flnd/docs/architecture.md` decided, but in a file only its owner can read (0600, in a 0700 folder), which it isn't today |
| portalocker for part locks | refused | flnd locks one byte far past a part's end, so ffmpeg and a source's `fetch` can still write the file. portalocker on Windows locks from offset 0. The hand-written lock is about 30 lines |
| puremagic for kinds | refused | `sniff` is 40 lines. puremagic spells and orders extensions differently |
| pathvalidate for names | refused | `filename`'s output may never change, or saved files download again under new names. pathvalidate deletes characters that `filename` turns into `_`. It is likely slower than the `cpu.filename` gate too |
| `shutil.unpack_archive` alone | partly | It is used for zip and tar (`format=` from the content check, since loi serves 7z files named `.zip`; tar's `data` filter). Staging, the verify step, size limits against zip bombs, and refusing `..` members stay, since the stdlib silently skips those |
| yt-dlp for cookies | replaced by the extension | Reading the cookie database from outside the browser fails under Chrome's app-bound encryption on Windows. On Linux it also needs `secretstorage` |
| python-jsonpath for `dig` | refused | 4× to 40× slower (see Ruled out) |
| ffmpeg reading stream URLs | refused | A cut stream would restart, and its requests would lose the Chrome fingerprint |

So "lean" comes from **cutting and simplifying**, not from swapping: compress, stats, the full
screen, link-following, the third Ctrl-C, segments, warm tabs, the old-home code, `_media`,
most of `_tools`, every dead parameter and shim, and the duplicates the review listed.

**Ruled out (searched or measured):**
- **A package for the AIMD cap** (searched 2026-11-17). gentlify, aioadaptive (Vegas, a
  Netflix concurrency-limits port), ace-concurrency, aimd-limiter (a rate limiter, not a
  concurrency one) and adaptive-concurrency each have 1 to 4 releases, put out within a day or
  two. adaptio has 15 releases over a year, but adjusts on load. None of them grows the cap
  only while throughput still rises, which is what flnd's `Cap` does. So it stays in-house,
  pulled out unchanged (`_net.py:470-624`, with `Place` and `Limits._most`).
- **JSONPath in place of `dig`** (measured on the `cpu.dig` bench page of 1,500 posts):

  | | wide read | one field | deep search, 2 MiB |
  | --- | --- | --- | --- |
  | `dig` | 47 µs | 0.5 µs | 1.6 ms |
  | python-jsonpath | 1,930 µs | 4.4 µs | 52 ms |
  | jsonpath-ng | 980 µs | 2.1 µs | 33 ms |
  | jsonpath-rust-bindings | ~2,000 µs | | 2.5 ms |

- **httpx:** it would lose curl_cffi's Chrome TLS fingerprint.
- **gallery-dl's extractors in place of our sources:** they can't drive a browser, and smart
  sync needs listings that flnd controls.

**Goal, corrected by the review** (before the signpost rule of Docs, which lowers it to about 11,000): `flnd/` and `flncore/` together, without `sources/`, at
**about 18,500 lines, from 30,400 (−39%)**. The first draft's 10,500 assumed dependency swaps
that didn't survive, and it forgot that AGENTS.md's docstrings are about half of every file.
By slice: look and run 3,050, net and transfer 2,400, browser and site 2,400, storage and
config 4,200, engine, job and source API 6,700. The sources go from 6,586 to about 5,500 with
the API 2 patterns below. Each estimate is checked as its step lands.

New dependencies: cyclopts and tomlkit. Leaving: Pillow, pyoxipng, psutil (and nothing replaces them).

## Module by module

What each module becomes, from the review of every line (2026-11-17). "→" is a move, and
"delete" lists what goes. Line numbers are on 49ff2dd.

| today | lines | becomes | about | what changes |
| --- | --- | --- | --- | --- |
| `cli.py` | 1,618 | `flncore/cli.py` (App, help formatter, exit codes, crash file, tables) + `flnd/cli.py` (commands) | 300 + 350 | Delete `_settle`, `parser`, `_globals`, the flag helpers, `retry`, `check`, `_RunLogStream`, `_Exec`, `_View`/`_Plain`/`_Json`, `_line`. The 95 literal colours become palette roles. Exit codes are looked up by class, not by name |
| `_ui.py` | 854 | `flncore/log.py` | 400 | Rewritten around `log.item`. Keep the palette, glyphs, `_SafeFormatter` and the level methods. Delete `Row`, `Group`, `_RowColumn`, the group registry, the aliases `UI.Group`/`UI.Row`, and `raw`. Check the level before formatting |
| `_prompt.py` | 286 | `flncore/prompt.py` | 120 | questionary only. Delete `_is_mock`, the three silent fallbacks and `_line`. `_paused` asks `log` to hold, instead of reaching into Rich's privates |
| `_run.py` | 593 | `flncore/run.py` + `flnd/_run.py` | 220 + 60 | Delete `suspend`, `resume`, stats, `restoring`, `quit_now`, the third stage. `Stop.stage` becomes an Enum |
| `_loop.py` | 337 | `flncore/loop.py` | 200 | Keep `run`, `submit`, `close`, `here`, `_carried`. Delete `running` and the helpers only stats used. `held` moves to `flncore/run.py` as `watch` |
| `_events.py` | 659 | `flnd/_events.py` | 430 | Delete the Stats family. Every row gets `tag=True`. `Planned.state`, `Skipped.reason`, `Login.state` and `Installed.origin` become Enums |
| `_library.py` | 917 | `flnd/_library.py` | 800 | The library API of The library. Delete `retry`, `ui`, `site`, the compress options |
| `errors.py` | 233 | `flncore/errors.py` + `flnd/errors.py` | 170 + 60 | `classify` → `Error.of`, which no longer writes to the caller's error. `disk` → `DiskError.of`. `Error.target` goes: a target lives on events. `Blocked` gets at most 5 options |
| `_net.py` | 2,368 | `flncore/net.py` + its fake in `flncore/testing.py` | 1,600 + 230 | Keep the policy, `Bucket`, `Cap`, `_Valve`, the writer and hasher threads. Delete the stats views, `suspended`, `_Segments` and `segments=`, and the mirror loop (mirrors are `_transfer`'s). `follow` → `resolve`, `fill` → `download`. Make `_checked` and `with_query` public. Cut the imports of flnd's `_html`, `_cookies` and `_text` |
| `_transfer.py` | 703 | `flnd/_transfer.py` + parts into `flncore/files.py` and `flncore/media.py` | 200 + 120 + 120 | Delete the dead `known` and `stop=` chains, the stats on `_Row`. `_Row`/`_Stream` → `log.Item`. `_fetch_again` retries only network errors |
| `_media.py` | 235 | deleted | 0 | Its only importer was `_shrink` |
| `_tools.py` | 462 | `run(argv, *, timeout)` + `shutil.which` in `flncore/files.py` | 70 | `MissingError` and `RunError` fold into `ToolError`, keeping the output's tail. Delete `stream`, `version` and its cache. Every run gets a deadline |
| `_stats.py`, `_shrink.py` | 1,118 | deleted | 0 | With every caller listed in the review |
| `_browser.py` | 2,101 | `flncore/browser.py` | 1,600 | Delete warm tabs, the no-Playwright fallbacks, `_with_query`, `RunBrowser.tabs`. Merge `_launch` and `_open_context`. `_Scroller`, `_Cursors` and `_matcher` become public. The Chromium offer is a callback, so `browser` never imports `prompt`. Add deadlines, a relaunch after a crash, and a check for dead idle tabs |
| `site.py` | 994 | `flnd/site.py` + `listing.py` | 700 | `Listing`, `paged` and `_first_page` → `listing.py`. Delete `back_off`, `warm`, `pause`, `scroll`, `SourceRun.load`. Stealth is applied per tab, so `stealth = False` finally works |
| `_files.py` | 1,012 | `flncore/files.py` | 650 | Keep the parts, `_lock`, `sniff`, `walk`, `link`, `sweep`. Delete `OLD_PARTS` and the `.stage`/`.info`/`.lock` leftovers, and the `mimetypes` fallback. Merge the second `digest` from `_vault`. `filename` moves in from `_text` |
| `_unpack.py` | 514 | `flncore/files.py` | 250 | zip and tar through `shutil.unpack_archive(format=, filter="data")`; keep staging, limits, verify and refusing `..`. Every `OSError` becomes `DiskError`. Refuse Windows device and stream names. Pass a 7z password through standard input, never on argv |
| `_cookies.py` | 1,126 | `flnd/_secret.py` | 250 | Keep `Jar`, the Netscape and JSON readers (the stdlib's drops `#HttpOnly_` lines). Every browser reader and decrypter (~700 lines) goes once the extension lands, with `refuse_profile` and `tests/no_profiles.py` |
| `_vault.py` | 601 | `flnd/_secret.py` | 150 | Delete the dead crypto, `Keychain`, `key`, the `key_path` shim and the bytes branch that returned ciphertext as a cookie. The store's file and folder get 0600 and 0700 |
| `_db.py` | 286 | `flnd/_db.py` | 220 | Keep. A `mode` for the secret store. `close` behaves as its docstring says |
| `_home.py` | 1,683 | `flnd/home.py` | 1,050 | Check `meta.version` on open, and refuse an old home with a hint. Move the `failures_key` index into `SCHEMA`. Delete `_upgrade`, the cookie-folder code, `probes`, `shrunk`, `failed_at`, the compress columns' readers. `record(follow: bool \| None)` → an Enum. `fail()` takes a `FailureRow` |
| `_check.py` | 283 | `flnd/home.py` | 180 | Only `FileNotFoundError` counts as missing. `fix` refuses when `media/` is missing. Delete adoption |
| `_html.py` | 512 | `flncore/dig.py` + `flnd/_html.py` | 200 + 220 | `dig` keeps its `lru_cache` and inlined loops. It raises flncore's `Changed`. Add `meta()`, shared by fbk and ins |
| `_text.py` | 392 | split | — | `filename` → files; `redact`, `size`, `duration`, `count` → log; `fill` and `fields` stay in flnd |
| `_time.py` | 263 | `flncore/clock.py` | 210 | `Clock` becomes a base class. `day()` reads the active clock. The raw `time.time()` and `date.today()` calls in `_db`, `_home`, `_check`, loi and tik go through it |
| `options.py` | 776 | `flncore/config.py` + `flnd/options.py` | 180 + 300 | One `--only` checker instead of two. `config.toml`'s structs in msgspec. Delete `Retry`, `selected`, the cut keys. The tomlkit writer (`set`, `remove`, checked against the struct, written atomically) goes in `flncore/config.py`, since any program can use it; flnd adds the `Setting` rows and the short keys (about 80 lines in all) |
| `job.py` | 3,209 | `listing.py` + `job.py` | 1,050 + 1,100 | The split the review gave (listing, cursors, gaps, retries, dating → `listing.py`). Delete `Board`, `Counts`, the dead parameters, link-following. `failure`, `tell`, `logged_out` → engine |
| `engine.py` | 1,785 | `engine.py` | 1,150 | Merge the two search paths, the two download paths and the three login gates. Delete `retry`, `_found`, `_compress_saved`, the board. Stop printing from the library |
| `_steps.py` | 876 | folded into `job.py` | 0 | Dedupe and the sidecar run inline. Unpacking runs after the post's rows, outside the download slots, through `to_thread` and one semaphore |
| `_plugins.py` | 363 | `plugins.py` | 330 | Delete entry points. Add `plugin install` (imported in a child process first) and `remove` |
| `source.py` | 2,239 | `source.py` | 2,050 | API 2 (below). `_best_streams` → `flncore/media.py`. Fix `_assign`'s writes to frozen posts |
| `testing.py` | 871 | `testing.py` | 650 | On `flncore.testing`. `FakeSite` takes a fixtures record. One `_first_key`, shared with the job |
| `sources/__init__.py` | 192 | same | 170 | Delete `copy`. Nothing iterates every plugin on a plain `sync` or `login` |

## Bugs the review found

Every one was found by reading the code on 49ff2dd, and each gets a test that fails before
its fix. Those marked *likely* were not traced end to end; their test decides whether they
are real.

**Data and history:**

| # | bug | where | fixed in step |
| --- | --- | --- | --- |
| 1 | A gap is lost after a resumed listing: the resumed pass marks the listing complete, then a cut "from the newest" pass writes no gap, so a later sync never reaches the posts between | `job.py:2739-2754` | 8 |
| 2 | A slow reader stalls every transfer and the browser: events are emitted on the loop and wait for room in the run's queue | `job.py:1648, 1687, 1695`; `_steps.py:592`; `_run.py:392` | 6 |
| 3 | A disk error while recording a post is lost: no `Failed` event, no failure row, and the run goes on, while the same error elsewhere stops it | `job.py:1594, 1636, 1447` | 6 |
| 4 | One transaction per post breaks under load: a post's first file can commit alone while the listing waits for room for the second | `job.py:1445` | 6 |
| 5 | The head stop fires one post too early: saved posts before the head count toward the streak | `job.py:2607` | 8 |
| 6 | `status --check --fix` can drop every row: any `OSError` counts as missing, and rows hold paths relative to where flnd ran, so an unmounted drive or another folder empties the history | `_check.py:117-123`; `job.py:1755` | 2 |
| 7 | An old home is read as a new, empty one (everything re-listed, logins lost); `meta.version` is never checked | `_home.py:96-98, 416, 462` | 2 |
| 8 | ins: posts put aside are never listed when the engine stops the listing early (*likely*) | `ins.py:691-700` | 9 |
| 9 | An error read back from `flnd.db` loses `retryable` | `_library.py:898` | 2 |

**Saved files:**

| # | bug | where | fixed in step |
| --- | --- | --- | --- |
| 10 | A DASH video is saved without sound, silently, when the audio part is empty or ffmpeg's stderr matches an "audio" pattern; it is recorded saved and never retried | `_transfer.py:348-383` | 2 |
| 11 | An invalid DASH video is downloaded 3 times before the progressive mirror is tried | `_transfer.py:387-419` | 2 |
| 12 | Retries multiply: 3 merge tries × 5 request tries per stream; a `Fetch` function retries even a `DiskError` | `_transfer.py:392-404, 422-447` | 2 |
| 13 | A resume trusts any 206 without reading `Content-Range`'s start (*likely*) | `_net.py:1344` | 2 |
| 14 | A part opened with `wait=True` can be written over after another run landed it | `_files.py:529-542` | 2 |
| 15 | On a terminal without `-v`, a saved file leaves no line at all: the run's level hides the bar's finished line | `cli.py:1220, 1446`; `_ui.py:584` | 4 |

**Network and browser:**

| # | bug | where | fixed in step |
| --- | --- | --- | --- |
| 16 | `Retry-After: 0` or a past date gives three 429s back to back | `_net.py:766, 1246` | 2 |
| 17 | A paced host with no rate gets no limit at all | `_net.py:742-747` | 2 |
| 18 | `stealth = False` is ignored in every run | `site.py:825, 581` | 9 |
| 19 | A 429 on a warmed page is warned, not waited out; warm loads skip the pace and block the listing thread for up to 15 s each | `site.py:437-454`; `_browser.py:1732` | 9 (warm goes) |
| 20 | No deadline on `content`, `evaluate`, `close`, `new_page`, `add_cookies`, `Capture.settle` or `Loop.run`; one stuck body holds back every later one | `_browser.py:866-1017, 1376`; `_loop.py:93` | 12 |
| 21 | A crashed Chromium is never relaunched; every later call raises Playwright's raw error to the source | `_browser.py:1041, 862` | 12 |
| 22 | A closed tab can be handed to the next target | `_browser.py:1498` | 12 |
| 23 | `Page.goto` swallows navigation errors into `None`, so a timed-out page is never retried | `_browser.py:801-805` | 12 |
| 24 | Cookies of different sites are merged by name, so another site's cookie can shadow a login's (*likely*) | `site.py:334-336` | 9 |

**Security:**

| # | bug | where | fixed in step |
| --- | --- | --- | --- |
| 25 | The secret store (cookies, the tlg session) is created 0644, readable by other local users | `_db.py:75-76`; `_vault.py:262` | 0 |
| 26 | A 7z password is passed on argv, where other processes can read it | `_unpack.py:403, 458` | 2 |
| 27 | An encrypted zip is listed with `zipfile`, which doesn't refuse symlinks, then extracted by 7zz, which may create them (*likely*) | `_unpack.py:218-224` | 2 |
| 28 | Windows device and stream names (`CON`, `a:b`) pass `_safe_dest` | `_unpack.py:253-262` | 2 |

**Sources:**

| # | bug | where | fixed in step |
| --- | --- | --- | --- |
| 29 | twt: `seen \| {""}` copies the whole set for every tweet, about 10⁸ copies on a 10,000-tweet timeline | `twt.py:524` | 0 |
| 30 | tlg: the app version is always "0" (it asks for `flnd`, but the package is `flntools`) | `tlg.py:944` | 0 |
| 31 | tlg: a temp file and a rename for every 512 KiB written | `tlg.py:682-686` | 9 |
| 32 | tik and twt: a 404 or a passing error in a single-item route becomes a warning, never recorded gone or failed; their tests pin it | `tik.py:668`; `twt.py:307` | 9 |
| 33 | tik: feeds opened outside `with` stay open when the load raises | `tik.py:245-246, 372-374, 424` | 9 |
| 34 | Null fields crash parsing: `.get` on non-dicts, `["url"]` without a check, `in` on a null `__typename` | `ins.py:1205-1247, 1329-1340`; `twt.py:704-747, 932`; `fbk.py:1079, 1119`; `loi.py:168, 462` | 9 |
| 35 | fbk: a shared post's media from the first batch lands in `posts/` | `fbk.py:795` | 9 |

Step 0 takes the three one-line fixes (25, 29, 30), so they ship before anything moves.

## What is removed

> "Things like compress maybe removed, make flnd an improved gallery-dl, yt-dlp … that
> supports things like browser"

- `--compress`, `[defaults] compress`, `[compress]`, `_shrink.py`, Pillow, pyoxipng, the
  photo and video compress lanes, `Chain.backlog`, `make media-real`, and the compress half
  of `flnd/docs/media.md`.
- The `ui` setting, the `Flnd.ui` property, and every leftover from the full screen.
- Live stats: `_stats.py`, psutil, `Run.stats`, `flnd.Stats` and the `Stats` JSON lines. The
  log line shows a file's own speed, so nothing else needs the meter.
- `flnd retry` and `flnd check` as commands (folded into `status`, see the CLI table).
- `Site.back_off` and `Site.warm` from the source API (see below).

Cut to make it shorter, each because it is a second way to do something, or rarely worth what
it costs:
- **`--exec`.** The `.jsonl` run log is already a stream of `Saved` events, and a script, or
  `jq` over `tail -f`, acts on it. That removes `_Exec`, its quoting rules, a page of `flnd/docs/cli.md`, and a
  place where text from a site comes near a command line.
- **Following links to other sites** (`Engine._found`, `FOLLOW_DEPTH`, up to two links deep).
  A listing that yields a `Target` for another site gets a one-line note in the log, which
  the user can `get`. That removes a recursive engine path and its dedupe of targets.
- **The third Ctrl-C.** The first stops listing and lets files in flight finish. The second
  quits at once: the parts stay, and the next run resumes them and finds any file already
  on disk. The undo registry (`restoring`, `quit_now`), which only the full screen needed,
  goes.
- **Plugin entry points.** Plugins come from the plugin folder (`flnd plugin install`) or
  `fl.add(cls)` in a script. That is two ways, each for its own user, instead of three.
- **`status --check --fix` adopting unknown files.** `--fix` only removes the rows of missing
  files and corrects sizes; an unknown file is reported, and a later `get` skips it anyway
  (the file is on disk).
- **`Run.suspend` and `Run.resume`** (the full screen's pause key), and **`Flnd.site()`**
  (a source's `Site` outside a run, used by nothing in the repo).
- **Old-home migration** in `_home.py` (see the table).
- **config.toml** keeps three keys, and every other key goes:

  ```toml
  [defaults]
  dedupe = "link"      # or "off"
  sidecars = false     # <file>.json beside each saved file

  [sources.ins]
  proxy = "socks5://127.0.0.1:1080"
  ```

  - `again`, `when`, `ui` and `compress` go with their flags.
  - `[compress]` goes with compression. `[vault] key` already has no reader since 49ff2dd;
    `flnd/docs/storage.md` still documents it.
  - `pace`, `scroll`, `limits` and `stop_after` go: a source declares them, and the AIMD
    cap adapts by itself.
  - A source's own keys are new: each source declares them (see the source API), and
    `config` shows them beside flnd's.
  - An unknown key stays a warning, not an error, so a config written for a newer flnd
    still runs.
  - **`flnd config init | set | remove`** (user, 2026-11-17). There is no `list`: the file
    is its own view, since it holds every key, commented.
    - **`init`** writes `<home>/config.toml` with every key commented out, under a line that
      says what it does and what it takes, and prints the file's path. Run again, it only
      appends the commented lines of keys the file lacks: new ones from a newer flnd, or
      from a plugin installed since. It never changes a value or a comment of yours, and it
      says what it added. The template is generated from the settings' classes, so the
      file, `flnd/docs/storage.md` and the code can't drift.
    - **`set <key> <value>`** runs `init` first when there is no file. It then writes the
      key in place: it replaces the key's commented line in its table, so the comment above
      stays with it. It checks the value with the same msgspec struct that reads the file,
      so a bad value never lands. An unknown key is refused, with the keys that exist.
    - **`remove <key>`** turns the line back into its commented default. It doesn't delete
      the line, so the file keeps showing every key.
    - **Keys** are short: `dedupe`, `sidecars`, and `<code>.<name>` for a source's own,
      such as `ins.proxy`.
    - **Comments survive:** edits go through **tomlkit**, which keeps a hand-written file's
      comments and layout. Checked on 2026-11-17: 57 releases since 2018, no dependencies
      of its own, runs on Python 3.11 and 3.14.
    - **Running syncs are safe:** the file is written atomically (a temp file, then a
      rename), and a run reads it once, at its start.
    - **`set`, not `add`:** every key already exists, with a default. `add` also already
      means "a source for this process" (`fl.add`).
    - `flnd status` prints the config file's path beside the home's.

  The file `init` writes, for a home with the built-in sources:

  ```toml
  # flnd settings for this home. Every key is shown with its default, commented out.
  # Change one with `flnd config set <key> <value>`, or edit this file by hand.

  [defaults]
  # How a file whose bytes are already saved is kept: "link" (a hard link) or "off" (a copy).
  # dedupe = "link"

  # Write <file>.json beside each saved file, with its post's caption, author and link.
  # sidecars = false

  [sources.ins]
  # A proxy for every HTTP request of Instagram: http://, https://, socks5:// or socks5h://.
  # proxy = ""

  [sources.tik]
  # A proxy for every HTTP request of TikTok: http://, https://, socks5:// or socks5h://.
  # proxy = ""

  # … one table per source, with `proxy` and the keys the source declares
  ```


Kept: dedupe, unpacking archives (the user said on 2026-11-13 not to remove it), sidecars,
`when`, `limit` and `dry` (in the library), searches (`mht?words`), smart sync whole (heads,
cursors, stacked gaps, retrying failures), plugins, and every source.

What flnd is next to gallery-dl and yt-dlp, which `flnd/docs/architecture.md#compared-with-similar-tools`
says in full: they read sites over HTTP. flnd also drives a real browser when a site needs
one, takes its login from the browser you already use, and keeps a target in sync cheaply
(one request when nothing is new, no gaps after a cut-short run, failed files brought back by
themselves). The plan sharpens those three things and drops what doesn't serve them.

## A source API that is hard to get wrong

> "Condense the flnd api to still keep current performance, but increase stability, and
> easier to write. Make it bullet-proof."

Source API 2 (`api = 2`). An `api = 1` plugin is refused, with a message that names what to
rename (`OLD_NAMES`): one spelling, no shims.

- **A block is waited out in place, never raised through the listing.** A category is a
  generator, and a generator that raised can't be resumed. For a scrolled feed, the engine
  would have to scroll back from the top. So:
  1. A source declares how to see a block in an answer: a hook
     `def blocked(self, answer) -> float | None` that returns the seconds to wait. It
     absorbs ins's `THROTTLED`, loi's refusal, fbk's `BLOCKED` and `COMET_ERROR`, and tik's
     captcha.
  2. `fetch`, `open` and `feed` call it inside the client's retry, so the request is waited
     out and repeated where it stands. The client already does this for a 429.
  3. The round waits (30 / 120 / 480 s) and the "fourth round raises" rule move into the net
     policy for a block without `Retry-After`.

  This replaces the 10 `site.back_off` calls and their attempt counters.
- **Declared login walls:** `walls = ("/accounts/login", "/challenge")`. `open` raises
  `LoginError` itself when it lands on one. This replaces fbk's `_on_login`, ins's `is_wall`
  and `suspended`, and twt's `WALLS`.
- **Pre-loading is declared:** `@route.category(url="{profile}/reels")`. The engine then
  pre-loads the next *wanted* category's page. Today `warm` (6 calls) pre-loads every later
  tab whatever the categories asked, skips the source's pace, and blocks the listing thread
  for up to 15 s each. Per-item pre-loads (ins's highlights) are dropped. The change is
  measured live before and after, since no offline bench covers it.
- **`Site` has 16 members**: 15 that a built-in source uses, and `settings` (below):
  - HTTP: `fetch`, `pages`;
  - the browser: `open` (returns the URL it landed on, which replaces `url`), `feed`,
    `document`, `click`, `handoff` (tik's captcha);
  - the run: `target`, `time`, `settings`, `cookies` (of the source's own domains only; today a
    same-named cookie of another site can shadow the login's), `state`, `secrets`, `client`;
  - talking: `log(level, message, **fields)`, which replaces `info` and `warn`, and `ask`.

  Gone: `back_off`, `warm`, `pause`, `url`, `html` (`flnd.source.parse` instead), and `wait`,
  `scroll`, `evaluate` and `tab`, which no built-in calls. `interactive` and `storage` stay
  on the login's site only.
- **Shared helpers instead of copies.** Each one replaces code that today is copied between
  sources:
  - `page.meta()` reads the `__bbox` results, which fbk and ins copy exactly;
  - `site.load(url, match, scroll=)` subscribes, loads, and gives the embedded results and
    then the responses (about ten copies in fbk, ins, tik and twt);
  - `page_info(path)` reads a `(has_next, end_cursor)` pair (fbk, ins, tik, twt);
  - `Post(date=, listed=)` separates the listing date from the naming date, which deletes
    ins's own file naming (about 80 lines);
  - a file stem whose extension comes from the content, and a class-level `file_headers`;
  - a resumable part for a `Fetch` function, with `write_at`. This deletes tlg's own part
    code, which today makes a temp file and a rename for every 512 KiB.
- **A source declares its own settings** (user, 2026-11-17), so `flnd config` shows and checks
  them like flnd's own:

  ```python
  class Instagram(Source):
      class Settings(source.Settings):
          """Instagram's settings.

          Attributes:
              covers: also save each highlight's cover image.
          """
          covers: bool = False
  ```

  - `source.Settings` is a frozen msgspec struct that already holds `proxy`, so every source
    has it. A source's subclass adds fields. Each field needs a default and a line under
    `Attributes:`, which becomes its comment in `config.toml`. A field without either is a
    `SourceError` when the class is made, naming the field.
  - The values reach the source as `site.settings`, typed: `site.settings.covers`. That makes
    16 `Site` members.
  - `flnd config init` and `set` know a plugin's keys once it is installed. A
    `[sources.<code>]` table for a source that isn't installed is a warning, not an error,
    so uninstalling a plugin never breaks a home.
  - No built-in declares a setting beyond `proxy` today. The first one that does comes with
    a test of its template line.
- **Declarations:**
  - `stealth` stays, applied per tab by an init script, so twt's `stealth = False` works at
    last.
  - `user_agent` goes. It is an HTTP setting, and the pinned Chrome/128 and Chrome/131
    strings contradict the fingerprint curl_cffi impersonates. Each source gets a live check.
  - `scroll` (unused by any built-in) goes.
  - Categories get `own=True|False` and `url=`.
- **Mistakes fail when the class is made**, as today. API 2 adds one check that can run
  then: a route without categories must be a generator function. The checks that depend on
  what posts hold at run time go into `flnd.testing.check`, and into a warning during the
  run. That covers a category listed by date without `newest_first`, and a template field
  the posts never fill.
- **Errors in single-item routes are no longer swallowed.** tik's embed and twt's tweet
  routes turn a 404 into a warning today, so nothing is recorded as gone or failed. Their
  tests pin that behaviour, and they are changed with it.

## Bullet-proof

Each item has a test that fails before its fix:

1. **The file's state machine** (above) is the only writer of a file's events and rows. A
   test drives every transition, including Ctrl-C at each one, and checks that no file ends
   without a final state.
2. **Every wait has a deadline.** Every `await` on the network or the browser has one, and
   so does each queue `get`. A transfer that gets no bytes for 30 s is retried from its part.
   A tab whose page hangs is closed and reopened once.
3. **The browser crashing is survivable.** If Chromium exits mid-run, it is relaunched once
   and the listing resumes from its cursor. A second crash fails the source's remaining
   targets with a clear hint.
4. **Kill -9 at any point loses nothing.** A test kills a worker process at random points of
   a fake run (seeded), restarts it, and checks the home with `fl.check()`. Parts resume, and
   no row points to a missing file.
5. **Fault injection in `core`:** `flncore.net`'s fake gains dropped connections, short reads,
   slow drips and wrong lengths, and `flncore.files` gains a full disk and a refused rename.
   Each fault ends as a typed `Error` with a hint, never a traceback.
6. **Hostile text:** a fuzz test feeds the log, file names and target text with
   escape codes, very long names, RTL marks and invalid UTF-8.
7. **A plugin can't take down a run.** A plugin that raises, hangs or holds the loop fails
   only its own targets (watchdog on `run_async`).

## Steps

Every step trims the docstrings and comments of the code it touches to signposts, and writes what it removed into the right `flnd/docs` page, so nothing a reader needs is lost.

Mechanical steps (moves, renames, import updates, snapshots) are delegated to a low-cost model
(AGENTS.md, Models). Design, the log, the state machine and the source API keep the stronger
model. Each step lists what it measures.

| # | step | kind | done when |
| --- | --- | --- | --- |
| 0 | **Baseline and cleanup.** Rename `docs/` to `flnd/docs/` (links, Makefile, `test_docs`, README) and rewrite AGENTS.md's "Writing" rules to the signpost rules of Docs. Finish removing `examples/`: take `examples` out of the Makefile's `lint`, delete `test_plugins.ExamplesTest` (moving anything it alone covered onto `tests/example_source.py`), and drop the mentions in README, AGENTS.md and `flnd/docs/writing-a-source.md`. Record `make bench` and line counts. Remove the dead arguments and the vault's legacy path (problem 8), and drop `run.log` (problem 12). Add a terminal-emulator test (pyte) that reproduces the drift on today's `_ui` | cleanup + design | `make check` green; the drift test fails, marked expected-failure |
| 1 | **Remove** compress, the `ui` setting, live stats, the `--help` timing test, and the cuts under "What is removed", with their config keys, deps, tests and docs | mechanical + docs | api.txt diff reviewed; Pillow, pyoxipng and psutil are gone |
| 2 | **Simplify in place**, one module per commit, still inside `flnd/`, following Module by module: `_tools` → `run`; `_unpack` on `shutil.unpack_archive` with its safety kept; `_files`, `_vault` (0600, dead crypto out), `_db`, `_home` (version check, `failures_key` in `SCHEMA`), `_check`, `options` on msgspec, `_net` (segments, stats views and the mirror loop out; `Cap` pulled out as it is), `_loop`, `_transfer`. Each commit passes the bench gate and builds `make dist` | design per module | each row of Module by module reaches its estimate, or its commit says why not |
| 3 | **Create `flncore/`** and move `_ui` → `log`, `_prompt` → `prompt`, `_run` → `run`, the base of `errors`). It then moves the slimmed `loop`, `clock`, `net`, `browser`, `media`, `files`, `dig` and `config` in as well, with their fakes into `flncore/testing.py`. `test_names`, `test_docs` and a `flncore-api.txt` snapshot cover it from this commit on. Update imports, `test_layers` rule 0, packaging, `make dist`, and the API snapshots | mechanical | `make check` and the bench gate pass; no behaviour change |
| 4 | **`flncore.log`**: items with stable keys, one renderer, no wrapping, capped region, and the `.jsonl` run log. The Charm Huh palette and glyphs move over | design | the drift test from step 0 passes; every line of a run's `.jsonl` decodes, and the file holds no `Progress` events |
| 5 | **`flncore.cli` and `flncore.prompt`**: cyclopts with a Charm Huh help formatter, error and hint printing, JSON lines and the exit-code table; questionary with the Charm style, and no test sniffing | design | `flncore-api.txt` reviewed; the help output is checked against a stored snapshot |
| 6 | **The file's state machine** in the job. Events and log items both come from it. `cli._Plain` and `_RunLogStream` go | design | a transition test covers every state and Ctrl-C |
| 7 | **The new CLI tree** (`get`, `sync`, `target`, `auth`, `status`, `plugin`) as annotated functions on `flncore.cli`, with only `-v`, `--all` and `-h`; categories in the target (`ins:example_user/reels`) and tab links; `own` categories; the library API of The library (`get(all, when, limit, dry)`, the collections `targets`, `auth`, `plugins`, `extension` and `config`, `flnd config init/set/remove` on tomlkit, and source-declared settings, no `again`); `make try` as `tests/try.py` | design + docs | `flnd/docs/cli.md` and `flnd/docs/library.md` match the code (`test_docs`); the API snapshot diff reviewed |
| 8 | **Slim the engine:** split `job.py`; the step chain becomes `to_thread`; trim `home.py` (old-home migration goes) | refactor | bench gate passes; line goals reported |
| 9 | **Source API 2:** engine-side backoff and pre-loading, the smaller `Site`, `Browser(...)`, and the new class checks. Migrate the 7 sources and `tests/example_source.py`. | design | every source test passes; the writing guide is rewritten |
| 10 | **The extension, part 1: logins.** `extension/` with `cookies` only, `flnd/_extension.py`, `flnd host`, `flnd extension install`/`remove` and `fl.extension` (copy the files, the host manifests for each Chromium browser and OS, the guided step, waiting for the connection), `auth login` through it. Then delete `_cookies.py` and its tests | design | the fake-extension protocol tests pass; `make ext` reads a test cookie from the local site; a live login per cookie source, by hand |
| 11 | **The extension, part 2: the browser.** The remaining ops, the backend switch in `site.py`, `Browser(real=True)`, and the popup's Get and Follow | design | every source's browser test runs on both backends against the fakes; live checks on fbk and ins, by hand |
| 12 | **Bullet-proofing** items 2–7 above, plus the audit of broad excepts (problem 13), the CLI's duck typing (11), and a test forbidding private access across modules (9) | design | each item's test is in `make check` |
| 13 | **Docs and handover:** the coverage test of Docs turned on for every page (each earlier step already keeps its own pages true); README points to `flnd/docs/start.md`; a last pass trims any docstring or comment still longer than a signpost; AGENTS.md (the dependency and writing rules). List the live hand checks per source for the user. Delete this file | docs | `make check` passes with the coverage test on; line counts reported against the goals |

Order:
- The removals (1) come first, so that nothing is swapped for a dependency only to be deleted.
- The simplifications (2) happen before the move (3), so the move carries small modules, and
  each diff reads on its own.
- The log (4) comes before the state machine (6), so the drift fix lands early and alone.
- The API breaks (7, 9) come late, once the inside is stable.
- The extension (10, 11) comes after source API 2, so its ops are written once, against the final `Site` verbs.

## Performance

**Does the new version keep the old performance?** Not by itself. It is *held* to the old
numbers by the 70 metrics in `tests/bench/gates.json`. `make bench ARGS=--gate` must pass
after every commit of step 2, and at steps 3, 4, 6, 8, 9, 11 and 12. A change that is more than
15% slower on a metric is not merged, however many lines it saves, unless this plan names
the cost and why it is accepted.

Measured on 2026-11-17, before any code changed (Python 3.14, Apple M5; scratch scripts
reproducing the bench cases):

| change | risk | how it is held |
| --- | --- | --- |
| No dependency on the hot path changes | none | tenacity, aiolimiter, anyio and portalocker were refused (see Custom code), largely for this reason |
| The AIMD cap, pulled out into one class | none: the code moves, and `CapTest` moves with it | `make cdn-real` once |
| Streams, as today (curl_cffi parts, then the ffmpeg merge) | none | `net.big_rate`, `net.stall_20mbs` |
| `log.Item.advance` on every 16 KiB chunk | about 33 ms per GiB (`cpu.advance_live`, 0.5 µs) | `bench/net.py` gets a progress sink, so `net.big_rate` sees this cost; today it passes none |
| The parts' lock and buffer | `disk.part_write` is 123 µs against a 130 µs target: 5.5% headroom | keep the 1 MiB buffer and the single `fstat` in `_land`; no wrapper around the lock |
| The store | `db.point_read` has 12% headroom, `db.commit_per_post` 13% | keep `Db.one`'s fast path and the single-statement insert; no layer around `_db` |
| `dig`'s move to `flncore` | `cpu.dig` 43.8 µs | keep the `lru_cache` on `_compile` and the inlined loops |
| The state machine and the event hand-off | **Today a slow reader stalls the loop** (bug 2 under Bugs): `emit` runs on the loop and waits for room | events from the loop go into a buffer the loop never waits on; the run's queue applies back-pressure only to the listing thread. Gated by `e2e.latency_500_posts` |
| curl_cffi upgrades | `_Valve`'s 8 MiB bound reads curl_cffi internals | pin `curl_cffi` to a major version, and add a contract test of the internals it uses |

Metrics that change on purpose:
- The `media.*` compress cases, the `ui.*` full-screen frames, and `startup.help`'s 40 ms
  target (startup is still measured and reported) go away with what they measured.
- New: `log.frame` (the renderer with 32 items in flight and 2,000 ended), with a budget of
  under 2% of `e2e.per_file`.

Rules that hold through every step:
- one asyncio loop for network and browser;
- no extra `stat` per file;
- one database transaction per post;
- the hashing writer thread for parts stays as it is.

The log renderer does no work per byte: a transfer bumps a counter, and the renderer reads
the counters at most 10 times a second. The two misses that are already documented
(1 GiB ≥ 4 GiB/s, and 2,000 small files ≤ 300 ms) stay documented misses. Nothing here is
allowed to widen them.

## Decisions

Each decision is made, with its reason. Revisit one only with a reason that is better.

1. **`--all` means every category**; the default is the account's own content (see The CLI).
2. **Live stats are removed.** They existed for the full screen, which is gone. A script
   still gets `Progress` events per category, and each log line shows its file's speed.
   psutil leaves the dependencies.
3. **`target remove` never deletes media files.** flnd deletes no file a person saved: not
   `--fix`, not `unfollow`, not `remove`. Deleting a folder is one `rm`, and an undo for it
   is impossible.
4. **`plugin install` takes a file or a folder.** It imports the plugin in a child process
   first, so a broken one is refused before it is copied in. Packages stay the installer's
   job (`uv tool install --with <pkg> flntools`), since flnd can't safely edit its own
   environment.
5. **The package is `flncore/`, not `core/`.** Every name in a wheel's top level is global to
   the Python environment it is installed into. A bare `core` clashes with any other project
   that ships one, and `import core` says nothing about where it comes from. `flncore` is
   still one word, sits at the root as asked, and matches the repo name `flntools`. (In this
   file, "core" means that package.)
6. **`retry` and `check` are not commands of their own.** Retrying happens on every run, and
   checking is a question about the home's state, so both belong to `status`.
7. **No `get --sync` shortcut.** It would be a second way to follow a target, and "one way to
   do one thing" wins over saving a command.
8. **cyclopts for the CLI, with our own help formatter.** Commands become annotated functions,
   and their help is their docstring: the "nicer API like annotations" without writing a
   parser. The look is ours, through `help_formatter`. Its startup cost (75 ms cold) is
   accepted now that the budget is gone.
9. **No yt-dlp.** Browser cookies come from the flnd extension, files from curl_cffi, and
   streams are merged by ffmpeg. `_cookies.py` stays as it is until the extension lands, then
   it is deleted.
10. **Dependencies were measured, and most were refused.** Only cyclopts and tomlkit are added. The others
    either changed behaviour (pathvalidate, portalocker, the stdlib unpacker on its own),
    couldn't be driven by the fake clock (aiolimiter), risked a gate (anyio), or saved only a
    few lines (tenacity, puremagic). Lean comes from cutting features and duplicates.
11. **`dig` stays.** JSONPath was tried (user, 2026-11-17), but no library came within 4× of
    `dig`'s speed (see Ruled out), and the user said to drop it if it was slow. A
    standard syntax isn't worth a 40× cost on every page a source reads.
12. **`flncore` is a kit for programs that fetch from the web**, not only flnd's look. It takes
    every module that adds a hard-won policy over its dependency (the test in Target shape).
    This replaces an earlier draft of this plan, which kept `net`, `browser` and `files`
    private as "glue". They aren't glue: the retry policy, the pacing, the parts and the
    hashing are the parts that are hard to get right.
13. **The secret store is plaintext, readable by its owner only** (0600 file, 0700 folder).
    Sealing it with a key flnd can always read protects nothing, and a keychain that cron
    can't unlock breaks syncs. Existing stores get their modes fixed when opened; nothing is
    re-written.
14. **Every file ends with an event.** The state machine's states are `queued`, `held` (waiting
    for a day or a link), `active`, `merging`, `unpacking`, and the ends `saved`, `linked`,
    `skipped`, `failed` and `dropped` (Ctrl-C). Today a file dropped by Ctrl-C, or one with no
    URL, ends with no event at all (`job.py:1591, 1699-1708`).
15. **`get` retries every failure of the targets it names**, however many runs failed them.
    `sync` keeps the cap of 3 runs per file. A person who names a target wants it whole, while
    cron shouldn't hammer a dead link. This replaces `flnd retry` without an option, and
    without the heavy "remove and list again".
16. **A second Ctrl-C quits within 2 s.** It cancels the transfers (the parts stay), writes
    nothing more, and gives the browser and the store 2 s to close before `os._exit(130)`.
    That is why a third press is never needed.
17. **An error no longer carries its target.** The events and the failure rows do. Today
    `classify` writes the target into the error itself, so an error shared by two targets
    keeps the first one's name (`errors.py:221`).
