# flnd from Python

What a script can call today, and what each call does. The CLI is built on the same calls, so
anything below works the same from the command line.

The rebuild replaces this API with `flnd.open(home)`, a `Flnd` object, runs and events. That is
not built yet: the commit that builds it rewrites this page. Until then, the names below are the
public library, and `tests/data/api.txt` lists each one with its signature.

## Reading targets

`flnd.resolve` reads a target the way the command line does, through its source's routes, and
gives a `flnd.Target`: the source's code, the route's kind and the captured value.

```python
import flnd

target = flnd.resolve("ins:example_user")
assert target == flnd.Target("ins", "user", "example_user")
assert str(target) == "ins:example_user"

# A link on the site's domains reads as the same target.
assert flnd.resolve("https://www.instagram.com/example_user/") == target
```

A text no source reads raises `flnd.TargetError`, whose message lists the forms that work:

```python
try:
    flnd.resolve("zzz:example_user")
except flnd.TargetError as error:
    print(error)        # unknown source 'zzz'. Sources: tik, fbk, ins, twt, loi, tlg, mht
```

`flnd.source(code)` makes the source itself, and `flnd.SOURCES` maps every code to its class:

```python
instagram = flnd.source("ins")
print(instagram.name, instagram.categories)
assert set(flnd.SOURCES) == {"tik", "fbk", "ins", "twt", "loi", "tlg", "mht"}
```

## Downloading

`flnd.download(targets, …)` downloads one target or several, one after another, and returns a
`flnd.Report` at the end. It draws the same progress on the terminal as the CLI.

<!-- docs-test: skip -->
```python
from pathlib import Path

import flnd

report = flnd.download(
    "ins:example_user",
    categories="posts",            # --from; "all" for every category
    time="2026-01-01,",            # --time; or a flnd.Range
    limit=None,                    # --limit
    dry=False,                     # --dry
    sync=True,                     # --sync: remember it for flnd sync
    again=False,                   # --again
    home=Path("media/.flnd"),      # the home folder; .flnd by default
)
print(report.ok, report.files)
for item in report.targets:
    print(item.target, len(item.written), item.skipped, item.error)
```

This block isn't run by the docs test, because it needs the network and a login. The rebuilt API
will run against the fakes.

| option | what it does |
| --- | --- |
| `categories` | `--from`: one name, several, or `"all"`; required for a profile, a chat or a search |
| `time` | `--time`: `"SINCE,UNTIL"` or a `flnd.Range`; both days are included |
| `limit` | at most this many posts per target, or targets per search; `None` for no limit |
| `dry` | list what would be saved and download nothing |
| `sync` | remember the targets for `Engine.sync` |
| `again` | ignore saved cursors and the stop at saved posts |
| `home` | the home folder (see [storage.md](storage.md)) |

`Report` holds one `flnd.TargetReport` per target, in order:

| name | what it is |
| --- | --- |
| `Report.targets` | the `TargetReport`s |
| `Report.files` | how many files were written, in all |
| `Report.failed` | the targets that failed |
| `Report.ok` | whether none failed |
| `TargetReport.target` | the target |
| `TargetReport.written` | the paths of the files written |
| `TargetReport.skipped` | how many files were already saved |
| `TargetReport.error` | why the target failed, or `None` |

A usage mistake (a bad `--from`, `--time` or `limit`) raises before anything is fetched. A
failure of one target is in its `TargetReport.error`; the other targets still run.

## The engine

`flnd.Engine(home=…)` has every command of the CLI as a method. The CLI makes one per command.

| method | CLI | what it does |
| --- | --- | --- |
| `resolve(texts)` | | resolve several targets |
| `download(targets, …)` | `flnd download` | as `flnd.download` above |
| `sync(targets=())` | `flnd sync` | download what is new for synced targets |
| `target_ls()` | `flnd target ls` | a `flnd.TargetInfo` per known target |
| `target_sync(targets, enabled=…)` | `flnd target sync-on`, `sync-off` | turn syncing on or off |
| `target_del(targets)` | `flnd target del` | forget targets, keeping their files |
| `auth_ls()` | `flnd auth ls` | a `flnd.AuthInfo` per source |
| `login(code, method, …)` | `flnd auth login` | log in by `"browser"` (with `browser=`) or `"file"` (with `path=`) |
| `logout(codes)` | `flnd auth logout` | remove logins |
| `logins` | | each source with a saved login, and its state |

The reads need no network, so they run anywhere:

```python
import flnd

engine = flnd.Engine()             # the home is .flnd in the current folder
assert engine.target_ls() == []    # nothing downloaded here yet
for info in engine.auth_ls():
    print(info.code, info.name, info.state)
```

## A source's site, from a script

`flnd.open(code, home=…)` gives the `Site` a source's routes act through, with the source's
saved login, pacing and retries. A script can make its own requests with it:

<!-- docs-test: skip -->
```python
import flnd

with flnd.open("ins") as site:
    page = site.fetch("https://www.instagram.com/example_user/").text
```

[writing-a-source.md](writing-a-source.md) lists what a `Site` offers.

## Errors

The library raises `flnd.TargetError` for a target no source reads, `flnd.LoginError` for a
missing login, and a usage error (`shared.cli.UsageError`, a `ValueError`) for a bad option.
[errors.md](errors.md) has the rest.
