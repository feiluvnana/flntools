# Cookbook

Practical recipes for the command line ([cli.md](cli.md)) and Python ([library.md](library.md)).
Every command below uses the example handles from the docs, and every Python block runs offline
in `tests/test_docs.py`.

## Twitter/X: tagged and mentioned media

X has no dedicated "tagged" tab, so a profile target (`twt:example_handle`) gets only that
account's own posts and replies. To capture photos and videos from other people's tweets that
mention the account, use a search target with X's search operators
([plugins/twt.md](plugins/twt.md)):

```sh
flnd get "twt?@example_handle -from:example_handle filter:media"
```

- `@example_handle` matches tweets mentioning the handle;
- `-from:example_handle` leaves out the account's own tweets;
- `filter:media` keeps only tweets with photos or videos.

Add `--when` so a busy handle does not scroll back through years of results (flnd translates
`--when` into X's `since:` and `until:` operators):

```sh
flnd get "twt?@example_handle -from:example_handle filter:media" --when 30d
```

Quote the target so the shell keeps the spaces and `-from:` inside one argument. A search target
requires a Twitter/X login (`flnd auth login twt`).

## Picking categories and opt-in tabs

Without categories, a profile downloads its site's own content and leaves out opt-in categories
marked `own=False` (such as `tagged` and `reposts` on Instagram, or `reposts` on Twitter/X).

Name specific categories after the last `/` to narrow a run, or use `/all` to include opt-in tabs:

```sh
flnd get ins:example_user/posts,reels
flnd get ins:example_user/tagged
flnd get ins:example_user/all
flnd get twt:example_handle/all
```

A target that is not a single post is followed automatically, and remembers the categories you
named for later syncs. Running `flnd get ins:example_user/posts` updates its saved categories to
`posts` alone.

## Syncing everyone you follow on Instagram

`ins:me` lists every account your logged-in Instagram account follows and downloads each one as its
own `user` target ([plugins/ins.md](plugins/ins.md)):

```sh
flnd auth login ins chrome
flnd get ins:me
```

Pass categories on `ins:me` to apply them to every followed account:

```sh
flnd get ins:me/posts,stories
```

`ins:me` requires an Instagram login (`sessionid` / `ds_user_id` cookie).

## Smart sync vs `--again` for unarchived posts

Normal `flnd get` (with no targets, to sync every followed target, or with a target name) uses
smart sync ([architecture.md](architecture.md#smart-sync)):

- `newest_first` categories (such as Instagram `posts`, `reels`, `reposts` or X `posts`, `media`)
  stop early once they reach the head seen on an earlier run;
- files that previously answered 404 or 410 are recorded as `gone` and skipped.

That keeps daily syncs fast:

```sh
flnd get
```

If a creator unarchives an older post below your last sync head, or a file that was temporarily
missing comes back, pass `--again` to walk past the saved head and cursors and ask again for `gone`
files:

```sh
flnd get ins:example_user --again
flnd get --again
```

Files already saved on disk are still skipped, so `--again` downloads only what is missing.

## Date windows and dry-run previews

Limit a run to a date window with `--when`: a year (`2026`), a month (`2026-06`), a day
(`2026-06-15`), a range (`2026-01..2026-03`, with either end optional), or a span back from today
(`30d`, `8w`, `1y`):

```sh
flnd get ins:example_user --when 30d
flnd get ins:example_user/reels --when 2026-01..2026-03
flnd get ins:example_user --when 2026-01..
```

If `config.toml` sets `[defaults] when`, pass `--when ""` to fetch every day for one run:

```sh
flnd get ins:example_user --when ""
```

Preview what a command would fetch before downloading anything by adding `--dry` (optionally with
`--limit` to sample a search or profile):

```sh
flnd get ins:example_user --dry
flnd get "twt?@example_handle filter:media" --limit 10 --dry
```

`--dry` writes nothing to disk or `flnd.db` and prints each file as `new` or `saved` (and each
search hit as `found`).

## Retrying failures and repairing the home

**Retrying failed files.** Every `flnd get` automatically retries that target's earlier failures
first, on up to 3 later runs per file ([errors.md](errors.md#retrying)). Once a file reaches that
cap, open the target list in a terminal and press `r` on the target to make its failures retryable
again, then sync:

```sh
flnd target list
flnd get ins:example_user
```

**Checking and repairing the store.** Run `flnd status` to see the last run's failures, accounts
that are held or expiring soon, and any mismatch between `flnd.db` and `media/`
([storage.md](storage.md#checking-a-home)):

```sh
flnd status
flnd status --json
```

When `flnd status` finds `missing` files (deleted from disk) or `wrong size` files on a terminal,
it asks `Repair N rows? (y/N)`. Confirming drops missing rows and updates resized ones without
deleting or downloading files. Follow up with `flnd get <target> --again` to re-download any files
whose missing rows were cleared.

## Multiple accounts and automatic switching

You can log in more than one account for the same plugin. Each login is sealed separately in the
vault ([storage.md](storage.md#the-vault)):

```sh
flnd auth login ins chrome
flnd auth login ins browser
flnd auth list
```

In a terminal, `flnd auth list` lets you press `*` to choose the plugin's default account.

To let flnd move a rate-limited, held, or refused request to the plugin's next healthy account
automatically, turn on `switch` in `config.toml` (`flnd config`, or edit `<home>/config.toml`):

```toml
[plugins.ins]
switch = true
```

Or set `switch = true` under `[defaults]` to enable account switching for every plugin.

## Python recipes

Every block below runs offline against a small in-memory plugin in a temporary home. Replace
`"dmo:example_user"` with a real target such as `"ins:example_user"` in your own scripts.

### Setup: a scratch home and offline plugin

```python
import datetime
import pathlib
import tempfile

import flnd
from flnd.plugin import File, Plugin, Post, route

home = tempfile.mkdtemp()


def text_file(name, body):
    data = body.encode()

    async def fetch(part):
        part.total(len(data))
        await part.write(data[part.offset:part.end])

    return File(fetch=fetch, name=name)


class Demo(Plugin):
    code, name, domains, api = "dmo", "Demo", ("demo.example.com",), 2

    @route("dmo:{user}")
    async def user(self, ctx, user):
        return user

    @user.category
    async def posts(self, ctx, user):
        yield Post("p2", [text_file("p2.txt", "newer")], date=datetime.date(2026, 3, 2))
        yield Post("p1", [text_file("p1.txt", "older")], date=datetime.date(2026, 2, 1))

    @user.category(own=False, newest_first=False)
    async def tagged(self, ctx, user):
        yield Post("t1", [text_file("t1.txt", "tagged")], date=datetime.date(2026, 3, 5))
```

### Dry-run preview with a date window and categories

Default targets skip opt-in (`own=False`) categories; pass `/all` or name the category to include
them:

```python
with flnd.open(home) as fl:
    fl.add(Demo)
    preview = list(fl.get("dmo:example_user/all", when="2026-03", dry=True))
    assert [(e.post, e.state) for e in preview] == [("p2", "new"), ("t1", "new")]
```

### Stream events and inspect the report

Iterate a `Run` to handle files as they land, or call `.wait()` when you only need the `Report`:

```python
with flnd.open(home) as fl:
    fl.add(Demo)
    saved = []
    run = fl.get("dmo:example_user")
    for event in run:
        match event:
            case flnd.Saved(path=path, post=post):
                saved.append((post, path.name))
            case flnd.Failed(error=error):
                raise RuntimeError(f"{error} ({error.hint})")
    assert sorted(saved) == [("p1", "p1.txt"), ("p2", "p2.txt")]
    assert run.report.ok and run.report.saved == 2
```

### Smart sync vs full re-scan (`again=True`)

A follow-up `fl.get()` with no targets syncs every followed target and stops as soon as it sees no
new posts. Open with `again=True` to walk the full listing again (the library equivalent of
`--again`):

```python
with flnd.open(home) as fl:
    fl.add(Demo)
    sync_report = fl.get().wait()
    assert sync_report.saved == 0 and sync_report.ok

with flnd.open(home, again=True) as fl:
    fl.add(Demo)
    rescan = fl.get("dmo:example_user").wait()
    assert rescan.saved == 0 and rescan.skipped == 2
```

### Check, repair, re-fetch a missing file, and reset retries

`fl.check(fix=True)` removes rows for files missing from disk and updates resized rows; a follow-up
`again=True` run downloads the missing file back. `fl.targets.retry(...)` resets any exhausted
failure counters for a target:

```python
with flnd.open(home) as fl:
    fl.add(Demo)
    first = fl.files(target="dmo:example_user")[0]
    first.path.unlink()

    before = fl.check()
    assert not before.ok and len(before.missing) == 1

    repaired = fl.check(fix=True)
    assert repaired.ok and repaired.fixed == 1

    fl.targets.retry("dmo:example_user")

with flnd.open(home, again=True) as fl:
    fl.add(Demo)
    refetched = fl.get("dmo:example_user").wait()
    assert refetched.saved == 1
```

### Multi-account login, default selection, and switching

Log in multiple accounts from cookie files (or browsers), pick the default account, and enable
automatic switching in `config.toml`:

```python
def write_cookies(filename):
    path = pathlib.Path(home, filename)
    path.write_text("# Netscape HTTP Cookie File\n"
                    ".instagram.com\tTRUE\t/\tTRUE\t2000000000\tsessionid\texample-value\n")
    return str(path)


with flnd.open(home) as fl:
    a1 = fl.auth.login("ins", path=write_cookies("c1.txt"), name="example_user")
    a2 = fl.auth.login("ins", path=write_cookies("c2.txt"), name="example_creator")
    assert a1.is_default and not a2.is_default

    fl.auth.default("ins", "example_creator")
    defaults = [acc.name for acc in fl.auth if acc.plugin == "ins" and acc.is_default]
    assert defaults == ["example_creator"]

    fl.config.set("plugins.ins.switch", True)
    assert fl.config["plugins.ins.switch"] is True
```
