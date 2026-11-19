# Glossary

The words every page of these docs uses, with what each means in flnd and the name the code
gives it. The first eight are the core words; the rest are the ones the other pages lean on.

## Target
Something to download. It is written `<plugin>:<what>`, as in `ins:example_user`, or given as a
link, as in `https://www.instagram.com/example_user/`. A search is written `<plugin>?<words>`, as
in `mht?example`. In code, a target is `flnd.plugin.Target(text)`. Resolving it sets its `kind`,
the name of the route that reads it, so a resolved target has its plugin's code, its kind and its
captured value (`example_user`).

## Plugin
The code for one site: one `flnd.plugin.Plugin` subclass. Its **code** is the prefix of its
targets, 2 to 16 lowercase letters and digits. The built-in plugins are `src/plugins/<code>.py`
(`ins`, `tik`, `twt`, `fbk`, `tlg`, `loi`, `mht`, and `web` for any other link), listed in
`src/plugins/_manifest.py`. `flnd.plugins.PLUGINS` maps every code to its class. See
[writing-a-plugin.md](writing-a-plugin.md).

An installed plugin is a `<code>.py` file, or a `<code>/` package, in the plugin folder. That
folder is `$FLND_PLUGINS`, else `%APPDATA%\flnd\plugins\` on Windows, else
`~/.config/flnd/plugins/`. `plugins.json` in that folder records each installed plugin. A plugin
is never kept in a home. See [writing-a-plugin.md](writing-a-plugin.md#10-shipping-it-as-a-plugin).

## Route
A method of a plugin that reads one kind of target, made with the `@route(...)` decorator of
`flnd.plugin`. Its patterns say which texts and links it accepts. Its method name is the target's
**kind** (`user`, `post`, `video`, `chat`). A route either yields posts itself (a single post's
link, with `single=True`), or returns a value its categories receive (a profile). Routes are tried
in the order the class declares them, and the first whose pattern matches reads the target.

## Category
One part of a target, such as a profile's `posts`, `reels` or `stories`. It is a method made with
`@<route>.category`, and it receives what its route returned. A target names its categories after
its last `/` (`ins:example_user/reels,posts`, or `/all`). Without them, a known target takes its
stored ones, and a new one takes its plugin's own. A single post has none.

## Post
One item a site lists: an id, a date, and one or more files (`flnd.plugin.Post`). A post's id
names its files and starts their history keys. A post whose files need extra requests may give a
function instead of a list, so the requests happen only for a post not saved yet (a **lazy**
post).

## File
One thing to download in a post (`flnd.plugin.File`): a URL, mirror URLs tried in order, a fetch
function (Telegram's), or a video URL with a separate audio URL (DASH), which flnd merges with
ffmpeg. `File.text(name, text)` is a small text file the plugin writes itself.

## Event
A report of something a run did. It is a file saved (`flnd.Saved`), skipped (`flnd.Skipped`) or
failed (`flnd.Failed`), or, for a dry run, a file planned (`flnd.Planned`). A script reads a run's
events by iterating it ([library.md](library.md)). What the run is doing now is its snapshot,
`run.state`.

## Home
The folder flnd keeps everything in: downloads, history, logins and settings. The library's
`flnd.open(home)` takes it. Without one, it is `$FLND_HOME`, else `.flnd` in the current folder,
which is also the CLI's. Its layout is in [storage.md](storage.md).

## Query
A route whose pattern is `<code>?{words}`, such as `mht?{words}`, typed as `mht?example`. It
yields other targets instead of posts, and each one found is downloaded as if it had been typed.
A route that isn't a query but yields targets too says `discovers=True`.

## Sync
A `get` of followed targets, which takes only what is new. On a newest-first listing it stops
after `stop_after` saved posts in a row (5 by default), or at the target's head. A `get` with no
target runs every followed target this way, and the engine calls that `run_sync`. A category
marked `newest_first` is the one that stops early. See
[architecture.md](architecture.md#smart-sync).

## Cursor
Where a paged listing got to: the site's page token, or the plugin's own. flnd saves each
category's cursor in `targets.cursors` when a download is cut short, and the next run resumes
from it, then catches up from the newest post.

## Head
The newest post of a `newest_first` category, as the last run that read it from the top saw it,
with no gap left behind. A sync stops after the head and the post after it, so a target with
nothing new costs one page. It is kept in `targets.heads`.

## Gap
Where a sync was cut short before it met the posts saved before it. A gap holds the cursor to
resume from, and is kept in `targets.gaps`. The next sync, once it meets saved posts on top, jumps
to that cursor and goes on there, so no post is left behind.

## Failure
A file or target that failed and isn't saved since: one row of `flnd.db`'s `failures`. `flnd
status` shows it, and the next download of its target retries it, up to three runs unless a retry
asks for every one. See [errors.md](errors.md#the-failure-report).

## Run
One `get` (the CLI command), or one `Flnd.get` call (`flnd.Run`). It starts when it is first
iterated or waited on, yields its events as they happen, and ends with a `flnd.Report`.

## Record
A saved file's row as the library reads it (`flnd.Record`): its path, size, post, category, day
and time of saving. `fl.files()` lists them, read from the history in `flnd.db`.

## Login
How a plugin logs in, and what it keeps: a cookie login keeps the site's cookies, and a plugin's
own method (Telegram's `qr` or `phone`, for example) keeps what its own login writes. A login is
kept in the home's vault, `metadata/secret.db`, sealed under the user's key. See
[storage.md](storage.md#the-vault).

## Account
One logged-in account of a plugin: a row of `flnd.db`'s `accounts`, named by its site, or as it
was given. A plugin's first account is its default, and a target runs as the account that last
worked for it. See [storage.md](storage.md).

## Pace
The least time between two page requests to a plugin's own domains, in seconds. `Plugin.pace` is
0.5 by default, and `pace` under `[plugins.<code>]` in `config.toml` replaces it. File downloads
aren't paced. They are capped per host instead.

## Bucket
A token bucket that spaces one plugin's page requests to one host, or to a named bucket, for one
account. It refills at the pace and holds one token at most. See `Bucket` in `src/engine/jobs.py`.

## Hold
A timed wait of an account, or of one bucket of it, when a site blocks it or asks it to wait. No
request goes out until the hold ends. An account held that way is marked `held` in `accounts`.

## Cap
The most file transfers at once to one host. It starts at 8 and grows to 32 while the host keeps
up. It halves when a transfer fails in the current round. See `Cap` in `src/engine/jobs.py`.

## Part
A file being written: `<name>.part` beside its destination. The part is its own lock. The run
writing it holds it, so no other run writes the same file, and it is renamed onto its real name
once whole. A failed download keeps its part, and the next run resumes it. A run removes the parts
no run holds and nothing has touched for a week. See [storage.md](storage.md#parts).

## Job
One piece of work and what it needs, in `src/engine/jobs.py`. A job is a page, a file, a tab, a
plugin call, a hash, an unpack, a merge, an exec or a write. The scheduler makes it wait for its
pools and holds, runs it, and retries it under the retry policy.

## Pool
A fixed number of places that one kind of job waits for. A job takes a place, and the jobs
behind it wait in line by priority, then by arrival. The size of each kind's pool is set in
`src/engine/jobs.py`.

## Lane
The place a kind of job waits in, in the order its jobs run. The scheduler's code calls a kind's
pool a lane. The unpack pool has one place, so archives unpack one at a time. The exec pool has
one, so the programs run in the order of the saves. flnd has no separate lane object.

## Journal
The JSON Lines file of one `flnd get`, `log/<time>_get.jsonl`. It holds an outcome record for each
file saved, skipped or failed, and the log lines written while the run went on. It is kept for 30
days. See [storage.md](storage.md#the-layout).

## Snapshot
What a run is doing now, as one copy: `run.state` (`flnd.State`). The run makes a new one 30 times
a second while something changes, so a reader keeps the one it holds for as long as it needs it.

## Switch
Moving a request that one account can't make to the plugin's next account, when `switch` is on for
that plugin. The target then goes on as that account. See
[storage.md](storage.md#settings-configtoml).
