# flnd from Python

A script uses flnd through one object: `flnd.open(home)` gives a `Flnd` for a home folder, and
its methods are the CLI's commands. A download returns a `Run`, which reports what it does as a
stream of events. Every `python` block on this page runs offline in the docs test, against a
small source defined below, so each one is a working example.

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

## A source to try things with

`fl.add(source)` adds a source for this `Flnd` alone. Here it is a two-category source whose
files are written by a fetch function, so nothing goes to the network.
[writing-a-source.md](writing-a-source.md) explains each part.

```python
import datetime

from flnd.source import File, Post, Source, target


def write(text):
    """A fetch function: it writes the file itself, instead of downloading a URL."""
    def fetch(dest, progress):
        dest.write_bytes(text.encode())
    return fetch


class Notes(Source):
    code, name, domains = "nts", "Notes", ("notes.example.com",)
    folder, key = "{category}", "{id}"

    @target("{user}")
    def user(self, site, user):
        return user

    @user.category
    def posts(self, site, user):
        for number in (1, 2):
            yield Post(f"p{number}", [File(write(f"post {number}"), name=f"p{number}.txt")],
                       date=datetime.date(2026, 3, number))

    @user.category
    def reels(self, site, user):
        yield Post("r1", [File(write("reel"), name="r1.txt")], date=datetime.date(2026, 4, 1))
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
| `flnd.Saved` | a file is downloaded and in place | `target`, `category`, `post`, `path`, `date`, `size` |
| `flnd.Skipped` | a file (or a whole post) is already saved | `target`, `category`, `post`, `path` (None for a post) |
| `flnd.Failed` | a file failed, or a whole target did | `target`, `error`, and for a file `post` and `path` |
| `flnd.Progress` | a category's counts changed, or its listing ended | `target`, `category`, `found`, `saved`, `skipped`, `failed`, `bytes`, `listed` |
| `flnd.Planned` | a dry run planned a file | `target`, `post`, `path`, `state` (`"new"` or `"saved"`) |

Events are frozen msgspec structs. A failed file is an event, never an exception: the run goes
on with the rest. `flnd.Failed.error` is a `flnd.Error`, whose `str()` is the message and `hint`
what to do.

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
| `bytes` | bytes downloaded |
| `planned` | files a dry run planned |
| `failed` | each `Failed` event, in order: a strict script checks it, or `report.ok` |
| `stopped` | whether `run.stop()` ended the run early |

### Options

Every option is keyword-only:

| option | what it does |
| --- | --- |
| `only` | the categories to take: `["reels"]`, or `"reels,posts"`. None (the default) takes every category. A single post has none, so `only` with one is a usage error |
| `when` | the days to take posts from: `"2026"`, `"2026-06"`, `"2026-06-15"`, `"2026-01..2026-03"` (either end may be left out), or a span back from today: `"30d"`, `"8w"`, `"1y"` |
| `limit` | at most this many posts per target, or targets per search |
| `compress` | shrink files as they land: **not built yet**, so True is a usage error |
| `dry` | plan instead: `Planned` events, and nothing is written |

```python
with flnd.open(home) as fl:
    fl.add(Notes)
    planned = list(fl.download("nts:example_user", only=["reels"], when="2026-04", dry=True))
    assert [(event.post, event.state) for event in planned] == [("r1", "saved")]
```

A mistake in a target or an option raises `flnd.UsageError` at the call, before the run exists:

```python
with flnd.open(home) as fl:
    fl.add(Notes)
    try:
        fl.download("nts:example_user", only=["stories"])
    except flnd.UsageError as error:
        print(error)        # no source here has 'stories': use all, or nts: posts, reels
```

### Reading a run

- **Breaking out of the loop pauses the run.** Iterating again goes on where it was, `wait()`
  finishes it, and leaving the `with` stops it. The listing waits while the script handles an
  event, so events never pile up in memory.
- **`run.stop()`** asks the run to stop as a first Ctrl-C does: listing stops, the files in
  progress finish, and the run ends normally with `report.stopped` set.
- **Ctrl-C** while a script reads a run on the main thread works in two stages: the first stops
  listing and finishes the files in progress, the second stops at once. Either way, reading the
  run then raises `flnd.Stopped`, a `KeyboardInterrupt`.
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
    fl.follow("nts:example_user", only=["posts"]).wait()   # later syncs take posts alone
    print(fl.sync().wait())                                # nothing new since
    fl.unfollow("nts:example_user")
```

| method | returns | what it does |
| --- | --- | --- |
| `follow(targets, only=, when=, compress=)` | `Run` | follow, then download; `only` is kept for later syncs, `when` bounds this download alone |
| `sync(targets=(), compress=)` | `Run` | download what is new for followed targets |
| `unfollow(targets)` | `list[Target]` | stop following; files and history stay |
| `retry()` | | **not built yet**: a failed file is retried by the next download or sync of its target |

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
| `fl.files(target=None, kind=None, since=None)` | a `flnd.Record` per saved file: `key`, `path`, `size`, `post`, `category`, `date`, `at`, and `kind` (`"image"`, `"video"`, `"audio"`) |
| `fl.logins` | a `flnd.Login` per source: `source`, `name`, `state`, `origin`, `expiring` |
| `fl.check(fix=False)` | **not built yet** |

## Logins and sources

| method | what it does |
| --- | --- |
| `fl.login(source, how)` | log in: `how` is a browser to read cookies from (`"chrome"`, `"firefox"`, `"edge"`, `"brave"`, `"chromium"`), a cookies.txt path, or the source's own way (`"qr"`, `"phone"` for tlg). Returns the source's `Login` |
| `fl.logout(source)` | remove a login; whether there was one |
| `fl.add(source)` | a source for this `Flnd` alone |
| `fl.site(source)` | a context manager giving the source's `Site`, with its saved login, pacing and retries, for a script's own requests |

<!-- docs-test: skip -->
```python
with flnd.open() as fl:
    fl.login("ins", "chrome")
    with fl.site("ins") as site:
        page = site.fetch("https://www.instagram.com/example_user/").text
```

This block isn't run by the docs test: it reads a real browser's cookies and asks a real site.

## The CLI's commands, through the library

| CLI today | library |
| --- | --- |
| `flnd download T --from C --time S,U --limit N --dry` | `fl.download(T, only=C, when="S..U", limit=N, dry=True)` |
| `flnd download T --sync` | `fl.follow(T)` |
| `flnd sync [T]` | `fl.sync(T)` |
| `flnd target ls` | `fl.targets()` |
| `flnd target sync-on T` | `fl.follow(T)` (which also downloads what is new) |
| `flnd target sync-off T`, `flnd target del T` | `fl.unfollow(T)` (files, history and the target's row stay) |
| `flnd auth login` | `fl.login(source, how)` |
| `flnd auth logout S` | `fl.logout(S)` |
| `flnd auth ls` | `fl.logins` |

`--again` has no library spelling: a run always skips what is saved, and the rebuilt CLI drops
the flag. Until the CLI is rebuilt on the library, a run also logs its progress lines on the
terminal, as the CLI does.

## Errors

Everything the library raises is a `flnd.Error` ([errors.md](errors.md)): `str(error)` says what
went wrong, and `error.hint` what to do. A usage mistake raises at the call. An error that stops
a whole run (a full disk) is raised when the run is read. A failure of one file or one target is
a `Failed` event instead, and the run goes on.

```python
with flnd.open(home) as fl:
    try:
        fl.check()
    except flnd.Error as error:
        print(type(error).__name__, error, "-", error.hint)    # UsageError check isn't built yet - …
```
