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
[writing-a-source.md](writing-a-source.md) explains each part. A source added this way comes
after the built-ins and the plugins, so a code one of them has is refused with a `UsageError`,
never replaced. To make a source every script and the CLI know, ship it as a plugin instead
([writing-a-source.md](writing-a-source.md#10-shipping-it-as-a-plugin)).

```python
import datetime

from flnd.source import File, Post, Source, target


def write(text):
    """A fetch function: it writes the file itself, instead of downloading a URL."""
    def fetch(dest, progress):
        dest.write_bytes(text.encode())
    return fetch


class Notes(Source):
    code, name, domains, api = "nts", "Notes", ("notes.example.com",), 1
    folder, key = "{category}", "{post}"

    @target("nts:{user}")
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
| `flnd.Saved` | a file is downloaded, and every step after it is done (dedupe, unpack, compress, the sidecar) | `target`, `category`, `post`, `path` (its final path: after `compress`, the compressed file's), `date`, `size` (as it is now) |
| `flnd.Skipped` | a file (or a whole post) is already saved | `target`, `category`, `post`, `path` (None for a post) |
| `flnd.Failed` | a file failed, or a whole target did | `target`, `error`, and for a file `post` and `path` |
| `flnd.Progress` | a category's counts changed, or its listing ended | `target`, `category`, `found`, `saved`, `skipped`, `failed`, `bytes`, `listed` |
| `flnd.Planned` | a dry run planned a file | `target`, `post`, `path`, `state` (`"new"` or `"saved"`) |
| `flnd.Stats` | about once a second, while the run is iterated | a snapshot of the whole run ([Live stats](#live-stats)) |

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
| `bytes` | the saved files' bytes, as they are on disk (after `compress`, compressed) |
| `planned` | files a dry run planned |
| `failed` | each `Failed` event, in order: a strict script checks it, or `report.ok` |
| `stopped` | whether `run.stop()` ended the run early |

### Options

Every option is keyword-only:

| option | what it does |
| --- | --- |
| `only` | the categories to take: `["reels"]`, or `"reels,posts"`. None (the default) takes every category. A single post has none, so `only` with one is a usage error |
| `when` | the days to take posts from: `"2026"`, `"2026-06"`, `"2026-06-15"`, `"2026-01..2026-03"` (either end may be left out), or a span back from today: `"30d"`, `"8w"`, `"1y"`; `""` for every day. None (the default) takes `when` under `[defaults]` in the home's `config.toml`, which bounds `sync` too |
| `limit` | at most this many posts per target, or targets per search |
| `compress` | shrink each photo and video as it lands, when a smaller copy looks the same, and remove each archive once it is extracted ([media.md](media.md)); `sync(compress=True)` also takes files saved before. None (the default) takes `compress` under `[defaults]` in `config.toml`, so `False` turns it off for one call |
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

### Live stats

While a script iterates a run, it yields a `flnd.Stats` snapshot about once a second, and
`run.stats` holds the latest (None before the first). It is what the CLI's full screen draws,
and `--json` prints it, so a script sees all the screen shows. Nothing is sampled while nobody
iterates: a `wait()` takes no sample, and psutil (for RAM and CPU) is imported only by the
first.

```python
class Slow(Source):
    code, name, domains, api = "slo", "Slow", ("slow.example.com",), 1
    folder, key = "{category}", "{post}"

    @target("slo:{user}")
    def user(self, site, user):
        return user

    @user.category
    def posts(self, site, user):
        site.pause(2.0, "the site's quiet hours")       # shows in the stats as a wait
        yield Post("s1", [File(write("slow"), name="s1.txt")], date=datetime.date(2026, 5, 1))


with flnd.open(home) as fl:
    fl.add(Slow)
    run = fl.download("slo:example_user")
    for event in run:
        if isinstance(event, flnd.Stats):
            row = event.listing[0]
            print(f"{event.elapsed:.0f}s: {row.target} {row.category} {row.state}, "
                  f"flnd uses {event.system.ram >> 20} MiB")
            for pause in event.pauses:
                print(f"  {pause.source} waits {pause.seconds:.1f}s: {pause.reason}")
    assert run.stats is not None and run.stats.listing[0].category == "posts"
```

| `Stats` field | what it is |
| --- | --- |
| `elapsed`, `saved`, `skipped`, `failed`, `bytes` | seconds since the start, and the report's counts so far |
| `speed` | bytes a second received over the last sample, every transfer together |
| `eta` | seconds left, once every listing is done and files are still to come; None before |
| `stopping` | `""`, `"soon"` after a first Ctrl-C or `run.stop()`, `"now"` after a second |
| `suspended` | whether `run.suspend()` holds the run |
| `transfers` | `running`, `queued` and `resuming` files; `hosts`, each with its `cap` (adaptive), `busy`, `waiting` and `most`; `files`, each in flight with `name`, `host`, `done`, `total`, `speed`, `resumed` |
| `listing` | per target and category: `pages` read by `site.pages`, `posts` seen, `found`, `saved`, `skipped`, `failed`, `bytes`, `streak` (saved posts in a row; 5 stops a sync early) and `state`: `listing`, `finishing`, `done`, `caught up`, `cut`, `stopped` or `failed` |
| `pauses` | each wait before a source's next request: `source`, `host`, `seconds` and `reason` (`Retry-After`, `back-off`, `pace`, or the source's own words) |
| `system` | `tabs` open and the `cap` one target holds; `browser`, the RAM of the browser's process tree; flnd's own `ram` and `cpu` (percent of one core); `ffmpeg`, `pillow` and `unpack` jobs running; `steps` waiting; free `disk` on the home's volume |

`run.suspend()` holds a run: no new request or file starts until `run.resume()`, and the files
in flight finish. A stop (Ctrl-C, `run.stop()`) ends a suspension, so a suspended run still
stops. The full screen's `p` key does this.

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
| `retry()` | `Run` | download again every file that failed, however many runs failed it ([errors.md](errors.md#retrying)) |

A download or sync is smart about what it already has ([architecture.md](architecture.md#smart-sync)):

- it first retries the target's files that failed on earlier runs (on at most 3 runs each),
  listing each failed post again for fresh links; a file it brings back is a `Saved` event
  with `retried=True`, and `report.retried` counts them;
- a sync with nothing new reads one page and stops: a `Skipped` event with
  `reason="nothing new"`, counted in `report.unchanged` (not in `report.skipped`, which counts
  files);
- a sync that was cut short is finished by the next one, which jumps to where it stopped: a
  `Progress` event with `note="resuming from the last run"`, counted in `report.resumed`.

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
| `fl.logins` | a `flnd.Login` per source: `source`, `name`, `state`, `origin`, `expiring` |
| `fl.status` | the last run that downloaded, as a `flnd.Status`: `command`, `started`, `ended`, `saved`, and `failures`, a `flnd.Failure` per file or target it left failed (`target`, `post`, `path`, `error`, `attempts`, `url`, `at`); `None` before the first run ([errors.md](errors.md#the-failure-report)) |
| `fl.check(fix=False)` | a `flnd.Checked`: the history against the disk. `missing` and `resized` (each a `Record`, as the history has it), `unknown` (paths under `media/` no row names), `files` (rows checked), `fixed`, and `ok`. `fix=True` makes the history match the disk, and never deletes, moves or downloads a file ([storage.md](storage.md#checking-a-home)) |

## Logins and sources

| method | what it does |
| --- | --- |
| `fl.login(source, how)` | log in: `how` is a browser to read cookies from (`"chrome"`, `"firefox"`, `"edge"`, `"brave"`, `"chromium"`), a cookies.txt path, or the source's own way (`"qr"`, `"phone"` for tlg). Returns the source's `Login` |
| `fl.logout(source)` | remove a login; whether there was one |
| `fl.add(source)` | a source for this `Flnd` alone; a code another source has is a `UsageError` |
| `fl.sources` | a `flnd.Installed` per source: the built-ins, the plugins (a disabled one too, with `disabled` saying why) and `add`'s. Each has `code`, `name`, `origin` (`"built-in"`, `"package"`, `"file"` or `"script"`), `where`, `version`, `api`, `login` (its state), `domains`, `forms`, `categories` and `logins` (its ways to log in). Reading it imports every plugin |
| `fl.site(source)` | a context manager giving the source's `Site`, with its saved login, pacing and retries, for a script's own requests |

```python
import pathlib

cookies = pathlib.Path(home, "example_cookies.txt")     # a cookies.txt export, made up here
cookies.write_text("# Netscape HTTP Cookie File\n"
                   ".instagram.com\tTRUE\t/\tTRUE\t2000000000\tsessionid\texample-value\n")

with flnd.open(home) as fl:
    assert fl.login("ins", str(cookies)).state == "logged in"
    with fl.site("ins") as site:
        assert sorted(site.cookies) == ["sessionid"]
        # site.fetch("https://www.instagram.com/example_user/") would now ask the real site,
        # with this login, Instagram's pacing and its retries.
```

The docs test runs this block offline: logging in from a cookie file reads the file and asks
no site. `fl.login("ins", "chrome")` reads a browser's cookies the same way.

## The CLI's commands, through the library

| CLI | library |
| --- | --- |
| `flnd download T --only C --when W --limit N --dry` | `fl.download(T, only=C, when=W, limit=N, dry=True)` |
| `flnd follow T` | `fl.follow(T)` |
| `flnd sync [T]` | `fl.sync(T)` |
| `flnd ls` | `fl.targets()` |
| `flnd unfollow T` | `fl.unfollow(T)` |
| `flnd login S HOW` | `fl.login(S, HOW)` |
| `flnd logout S` | `fl.logout(S)` |
| `flnd login` (no source) | `fl.logins` |
| `flnd check [--fix]` | `fl.check(fix=…)` |
| `flnd status` | `fl.status` |
| `flnd retry` | `fl.retry()` |
| `--exec CMD` | `for event in run:` on each `flnd.Saved` |

`--again` (the CLI's flag on `download` and `follow`) is `flnd.open(home, again=True)`: every
`download` and `follow` then lists each post again and asks again for files the site answered
404 or 410 for (recorded as gone, [storage.md](storage.md)). Without it, `again = true` under
`[defaults]` in the home's `config.toml` does the same. The same table turns off dedupe
(`dedupe = "off"`), turns on sidecars (`sidecars = true`), and gives `compress` and `when` their
defaults; `[sources.<code>]` sets a source's pace, scroll, limits and proxy
([storage.md](storage.md#settings-configtoml)). An argument given to a call always wins over
the file.
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
