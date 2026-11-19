# flnd from Python

A script uses flnd through one object: `flnd.open(home)` gives a `Flnd` for a home folder, and
its methods are the CLI's commands. A download returns a `Run`, which reports what it does as a
stream of events. Every `python` block on this page runs offline in the docs test, against a
small plugin defined below, so each one is a working example.

```python
import tempfile

import flnd

home = tempfile.mkdtemp()                       # the examples below share this home

with flnd.open(home) as fl:                     # a Flnd; nothing is written yet
    print(fl.targets(), fl.files())             # [] []: a new home knows nothing
```

## The home

`flnd.open(home)` takes the home folder, the one place flnd keeps downloads, history and logins
([storage.md](storage.md)). `~` is expanded. Without one it takes `$FLND_HOME`, else `.flnd` in
the current folder, as the CLI does. Opening a folder writes nothing; the first command that
saves something makes the layout.

A `Flnd` is a context manager. Leaving the `with` (or calling `fl.close()`) stops every run still
going, as a first Ctrl-C does, and waits for it. A run made but not started can't start after.

## A plugin to try things with

`fl.add(Notes)` adds a plugin for this `Flnd` alone. Here it is a two-category plugin whose
files are written by a fetch function, so nothing goes to the network.
[writing-a-plugin.md](writing-a-plugin.md) explains each part. A plugin added this way comes
after the built-ins and the installed plugins, so a code one of them has is refused with a
`UsageError`, never replaced. To make a plugin every script and the CLI know, ship it as a plugin
instead
([writing-a-plugin.md](writing-a-plugin.md#10-shipping-it-as-a-plugin)).

```python
import datetime

from flnd.plugin import File, Plugin, Post, route


def note(name, body):
    """A file the plugin writes itself: its fetch function writes into a `Part`."""
    data = body.encode()

    async def fetch(part):
        part.total(len(data))
        await part.write(data[part.offset:part.end])

    return File(fetch=fetch, name=name)


class Notes(Plugin):
    code, name, domains, api = "nts", "Notes", ("notes.example.com",), 2

    @route("nts:{user}")
    async def user(self, ctx, user):
        return user

    @user.category
    async def posts(self, ctx, user):
        for number in (1, 2):
            yield Post(f"p{number}", [note(f"p{number}.txt", f"post {number}")],
                       date=datetime.date(2026, 3, number))

    @user.category
    async def reels(self, ctx, user):
        yield Post("r1", [note("r1.txt", "reel")], date=datetime.date(2026, 4, 1))
```

## Downloading: runs and events

`fl.download(targets, …)` returns a `Run`. **Nothing happens until it is read**, like a
generator: iterating it starts it, and yields each event as it happens.

```python
with flnd.open(home) as fl:
    fl.add(Notes)
    for event in fl.download("nts:example_user"):
        match event:
            case flnd.Saved(path=path, date=date):
                print("saved", path.name, date)
            case flnd.Failed(error=error):
                print("failed", error, error.hint)
```

| event | when | fields |
| --- | --- | --- |
| `flnd.Saved` | a file is downloaded, and every step after it is done (dedupe, unpack, the sidecar) | `target`, `category`, `post`, `path` (its final path: on disk), `date`, `size` (as it is now) |
| `flnd.Skipped` | a file (or a whole post) is already saved | `target`, `category`, `post`, `path` (None for a post) |
| `flnd.Failed` | a file failed, or a whole target did | `target`, `error`, and for a file `post` and `path` |
| `flnd.Planned` | a dry run planned a file | `target`, `post`, `path`, `state` (`"new"` or `"saved"`) |

Events are frozen msgspec structs. A failed file is an event, never an exception: the run goes
on with the rest. `flnd.Failed.error` is a `flnd.Error`, whose `str()` is the message and `hint`
what to do.

There are no hooks: to act on each saved file, act on its `Saved` event, which comes once the
file is final. The CLI's `--exec CMD` does exactly that ([cli.md](cli.md#running-a-command-per-file));
a script that runs a program per file should pass `event.path` as one item of an argument list
(`subprocess.run(["tool", str(event.path)])`), never through a shell, since the name comes from
a site.

`run.wait()` runs to the end without the events and returns the `flnd.Report`; `run.report`
holds the counts so far at any time.

```python
with flnd.open(home) as fl:
    fl.add(Notes)
    report = fl.download("nts:example_user").wait()
    assert (report.saved, report.skipped, report.ok) == (0, 3, True)   # saved above already
```

| `Report` field | what it is |
| --- | --- |
| `saved`, `skipped` | files downloaded, and files (or posts) already saved |
| `bytes` | the saved files' bytes, as they are on disk |
| `planned` | files a dry run planned |
| `failed` | each `Failed` event, in order: a strict script checks it, or `report.ok` |
| `stopped` | whether `run.stop()` ended the run early |

### What a run is doing now

`run.state` is the run's snapshot: a frozen `flnd.State`, swapped in every 100 ms while
something changed, so a script reads it from any thread, and keeps one as long as it likes.
`version` goes up with each new one; before the run starts it is an empty one, at 0. The CLI's
screen is drawn from it alone.

| `State` field | what it is |
| --- | --- |
| `version`, `elapsed` | the snapshot's number, and the seconds since the run started |
| `targets` | one `flnd.TargetState` per target: `text`, `categories`, `phase` (a `flnd.Phase`: `WAITING`, `LISTING`, `DOWNLOADING`, `DONE`, `FAILED`), `found`, `saved`, `skipped`, `failed`, `bytes`, `speed` |
| `files`, `more` | the files in flight, oldest first, at most 6 (`flnd.FileState`: `target`, `path`, `done`, `total`, `speed`), and how many more there are |
| `holds` | the waits going on (`flnd.HoldState`: `plugin`, `account`, `bucket`, `until`, `reason`, `count`) |
| `now` | what the run does, in one line: the newest listing step, and the wait going on |
| `saved`, `skipped`, `failed`, `bytes`, `speed` | the whole run's counts; speeds are bytes a second over the last 3 s |

```python
with flnd.open(home) as fl:
    fl.add(Notes)
    run = fl.download("nts:example_user")
    assert run.state.version == 0
    run.wait()
    assert [(row.text, row.phase) for row in run.state.targets] == [
        ("nts:example_user", flnd.Phase.DONE)]
```

### Options

Every option is keyword-only. The categories go in the target, after its last `/`
(`"nts:example_user/reels,posts"`, or `/all` for every one); without them, a target the home
knows takes the ones it was followed with, and a new one its plugin's own. A single post has
none, so naming one for it is a usage error.

| option | what it does |
| --- | --- |
| `when` | the days to take posts from: `"2026"`, `"2026-06"`, `"2026-06-15"`, `"2026-01..2026-03"` (either end may be left out), or a span back from today: `"30d"`, `"8w"`, `"1y"`; `""` for every day. None (the default) takes `when` under `[defaults]` in the home's `config.toml`, which bounds `sync` too |
| `limit` | at most this many posts per target, or targets per search |
| `dry` | plan instead: `Planned` events, and nothing is written |

```python
with flnd.open(home) as fl:
    fl.add(Notes)
    planned = list(fl.download("nts:example_user/reels", when="2026-04", dry=True))
    assert [(event.post, event.state) for event in planned] == [("r1", "saved")]
```

A mistake in a target or an option raises `flnd.UsageError` at the call, before the run exists:

```python
with flnd.open(home) as fl:
    fl.add(Notes)
    try:
        fl.download("nts:example_user/stories")
    except flnd.UsageError as error:
        print(error)        # nts has no category stories
```

### Reading a run

- **Breaking out of the loop pauses the run.** Iterating again goes on where it was, `wait()`
  finishes it, and leaving the `with` stops it.
- **A slow reader paces the run.** At most 256 events wait for the reader; past that, the
  run's next event waits for room, and the run waits with it, so events never pile up in
  memory. Nothing waits for room while it holds a place another file needs: the transfers
  already started finish their bytes meanwhile.
- **`run.stop()`** asks the run to stop as a first Ctrl-C does: listing stops, the files in
  progress finish, and the run ends normally with `report.stopped` set.
- **Ctrl-C** while a script reads a run on the main thread works in three stages: the first
  stops listing and finishes the files in progress, the second stops at once (and warns that a
  third quits), and the third ends the process at once with exit status 130 (`os._exit`: no
  `finally` or `atexit` of the script runs). After the first or second, reading the run raises
  `flnd.Stopped`, a `KeyboardInterrupt`.
- **A run nobody starts** warns with a `ResourceWarning` when it is dropped, the way a coroutine
  nobody awaited does: it is almost always a missing `.wait()`.
- **From asyncio code,** `async for event in run` and `await run` do the same without blocking
  the event loop: the work runs on the run's own thread either way.

```python
import asyncio


async def main():
    with flnd.open(home) as fl:
        fl.add(Notes)
        return await fl.download("nts:example_user")

assert asyncio.run(main()).skipped == 3
```

## Following and syncing

`download` takes what is there once. `follow` records the targets, then downloads them like
`download`; `sync` then takes what is new for every followed target (or the ones named), and
`unfollow` stops following, keeping the files and the history.

```python
with flnd.open(home) as fl:
    fl.add(Notes)
    fl.follow("nts:example_user/posts").wait()             # later syncs take posts alone
    print(fl.sync().wait())                                # nothing new since
    fl.unfollow("nts:example_user")
```

| method | returns | what it does |
| --- | --- | --- |
| `follow(targets, when=)` | `Run` | follow, then download; the categories each target names are kept for later syncs, `when` bounds this download alone |
| `sync(targets=())` | `Run` | download what is new for followed targets |
| `unfollow(targets)` | `list[Target]` | stop following; files and history stay |
| `retry()` | `Run` | download again every file that failed, however many runs failed it ([errors.md](errors.md#retrying)) |

A download or sync is smart about what it already has ([architecture.md](architecture.md#smart-sync)):

- it first retries the target's files that failed on earlier runs (on at most 3 runs each),
  listing each failed post again for fresh links; a file it brings back is a `Saved` event
  with `retried=True`, and `report.retried` counts them;
- a sync with nothing new reads one page and stops: a `Skipped` event with
  `reason="nothing new"`, counted in `report.unchanged` (not in `report.skipped`, which counts
  files);
- a sync that was cut short is finished by the next one, which jumps to where it stopped,
  counted in `report.resumed`.

```python
with flnd.open(home) as fl:
    fl.add(Notes)
    report = fl.retry().wait()           # nothing failed in this home
    print(report.retried, report.ok)     # 0 True
```

## Reading the home

```python
with flnd.open(home) as fl:
    fl.add(Notes)
    [tracked] = fl.targets(source="nts")
    print(tracked.target, tracked.follow, tracked.files, tracked.categories)
    for record in fl.files(target="nts:example_user", since="2026-03"):
        print(record.path.name, record.size, record.date)
```

| read | returns |
| --- | --- |
| `fl.targets(source=None)` | a `flnd.Tracked` per target the home knows: `target`, `follow`, `at` (last sync), `files`, `size`, `seen` (for a search), `categories` (each one's state), `query` |
| `fl.files(target=None, kind=None, since=None)` | read from the home's `flnd.db`: a `flnd.Record` per saved file: `key`, `path`, `size`, `post`, `category`, `date`, `at`, and `kind` (`"image"`, `"video"`, `"audio"`) |
| `fl.logins` | a `flnd.Login` per plugin: `source` (its code), `name`, `state`, `origin`, `expiring` |
| `fl.status` | the last run that downloaded, as a `flnd.Status`: `command`, `started`, `ended`, `saved`, and `failures`, a `flnd.Failure` per file or target it left failed (`target`, `post`, `path`, `error`, `attempts`, `url`, `at`); `None` before the first run ([errors.md](errors.md#the-failure-report)) |
| `fl.check(fix=False)` | a `flnd.Checked`: the history against the disk. `missing` and `resized` (each a `Record`, as the history has it), `unknown` (paths under `media/` no row names), `files` (rows checked), `fixed`, and `ok`. `fix=True` makes the history match the disk, and never deletes, moves or downloads a file ([storage.md](storage.md#checking-a-home)) |

## Logins and plugins

| method | what it does |
| --- | --- |
| `fl.login(source, how)` | log in: `how` is `"chrome"` (extension), `"firefox"`, `"safari"` (macOS), a cookies path, or the plugin's way (`"qr"`, `"phone"` for tlg); returns `Login` |
| `fl.logout(source)` | remove a login; whether there was one |
| `fl.add(source)` | a plugin class for this `Flnd` alone; a code another plugin has is a `UsageError` |
| `fl.sources` | a `flnd.Installed` per plugin: the built-ins, the installed plugins (a disabled one too, with `disabled` saying why) and `add`'s. Each has `code`, `name`, `origin` (`"built-in"`, `"package"`, `"file"` or `"script"`), `where`, `version`, `api`, `login` (its state), `domains`, `forms`, `categories` and `logins` (its ways to log in). Reading it imports every plugin |

```python
import pathlib

cookies = pathlib.Path(home, "example_cookies.txt")     # a cookies.txt export, made up here
cookies.write_text("# Netscape HTTP Cookie File\n"
                   ".instagram.com\tTRUE\t/\tTRUE\t2000000000\tsessionid\texample-value\n")

with flnd.open(home) as fl:
    assert fl.login("ins", str(cookies)).state == "logged in"
```

The docs test runs this block offline: logging in from a cookie file reads the file and asks
no site. `fl.login("ins", "chrome")` reads a browser's cookies the same way.

## The CLI's commands, through the library

| CLI | library |
| --- | --- |
| `flnd download T/C --when W --limit N --dry` | `fl.download("T/C", when=W, limit=N, dry=True)` |

| `flnd follow T` | `fl.follow(T)` |
| `flnd sync [T]` | `fl.sync(T)` |
| `flnd ls` | `fl.targets()` |
| `flnd unfollow T` | `fl.unfollow(T)` |
| `flnd login S HOW` | `fl.login(S, HOW)` |
| `flnd logout S` | `fl.logout(S)` |
| `flnd login` (no plugin) | `fl.logins` |
| `flnd check [--fix]` | `fl.check(fix=…)` |
| `flnd status` | `fl.status` |
| `flnd retry` | `fl.retry()` |
| `--exec CMD` | `for event in run:` on each `flnd.Saved` |

`--again` (the CLI's flag on `download` and `follow`) is `flnd.open(home, again=True)`: every
`download` and `follow` then lists each post again and asks again for files the site answered
404 or 410 for (recorded as gone, [storage.md](storage.md)). `[defaults]` in the home's
`config.toml` turns off dedupe (`dedupe = false`), turns on sidecars (`sidecars = true`),
and gives `when` its default; `[plugins.<code>]` sets a plugin's pace, limits, proxy and
templates ([storage.md](storage.md#settings-configtoml)). An argument given to a call
always wins over the file.
A run draws nothing on the terminal:
the CLI draws it from the run's events, and flnd's own warnings (a wait a site asked for, a
login about to expire) go to standard error.

## Errors

Everything the library raises is a `flnd.Error` ([errors.md](errors.md)): `str(error)` says what
went wrong, and `error.hint` what to do. A usage mistake raises at the call. An error that stops
a whole run (a full disk) is raised when the run is read. A failure of one file or one target is
a `Failed` event instead, and the run goes on.

```python
with flnd.open(home) as fl:
    try:
        fl.download("zzz:example_user")
    except flnd.Error as error:
        print(type(error).__name__, error, "-", error.hint)    # UsageError unknown source 'zzz'…
```
