# Glossary

The words every page of these docs uses, with what each means in flnd today. Where the rebuild
changes a word's meaning, the entry says so.

## Target
Something to download. It is written `<source>:<what>`, as in `ins:example_user`, or given as a
link, as in `https://www.instagram.com/example_user/`. A search is written `<source>?<words>`, as
in `mht?example`. In code, a target is `flnd.source.Target(source, kind, value)`: the source's
code, the name of the route that read it, and the captured value (`example_user`).

## Source
The code for one site, as one `flnd.source.Source` subclass in `flnd/sources/<code>.py`. Its
**code** is the three-letter prefix of its targets (`ins`, `tik`, `twt`, `fbk`, `tlg`, `loi`,
`mht`). `flnd.sources.SOURCES` lists every built-in source. See
[writing-a-source.md](writing-a-source.md).

## Route
A method of a source that reads one kind of target, made with the `@target(...)` decorator. Its
patterns say which texts and links it accepts, and its method name is the target's **kind**
(`user`, `post`, `video`, `chat`). A route either yields posts itself (a single post's link) or
returns a context for its categories (a profile).

## Category
One part of a target, such as a profile's `posts`, `reels` or `stories`. It is a method made
with `@<route>.category`, and it receives what its route returned. Today the CLI picks
categories with `--from`; `--from all` takes every one. The rebuild renames the flag to `--only`.

## Query
A route whose pattern starts with `?`, such as `mht?{words}`. It yields other targets instead of
posts, and each one found is downloaded as if it had been typed.

## Post
One item a site lists: an id, a date, and one or more files (`flnd.source.Post`). A post's id
names its files and starts their history keys. A post whose files need extra requests may give a
function instead of a list, so the requests happen only for a post not saved yet (a **lazy**
post).

## File
One thing to download in a post (`flnd.source.File`): a URL, mirror URLs tried in order, a fetch
function (Telegram's), or a video URL with a separate audio URL (DASH), which flnd merges with
ffmpeg. `File.text(name, text)` is a small text file the source writes itself.

## History
What flnd remembers about each saved file, so a second run skips it. Today it is the `downloads`
table of `<home>/metadata/history.db`, one row per file, keyed `<code>:<key>` (see
[storage.md](storage.md)). A file's **key** comes from the source's `key` template, `{id}_{num}`
by default.

## Sync
Downloading only what is new. A target downloaded with `--sync` is remembered as synced, and
`flnd sync` downloads what is new for every synced target. On a sync, a category marked
`newest_first` stops after 5 saved posts in a row.

## Cursor
Where a paged listing got to. flnd saves each category's cursor when a run is interrupted, and the
next run resumes from it, then catches up from the newest post.

## Event
A report of something a run did: a file saved (`flnd.Saved`), skipped (`flnd.Skipped`) or
failed (`flnd.Failed`), the counts so far (`flnd.Progress`), or, for a dry run, a file planned
(`flnd.Planned`). A script reads a run's events by iterating it ([library.md](library.md)).
Today's CLI still draws its own progress lines; the rebuilt CLI draws them from the events.

## Run
One `download`, `follow` or `sync` of the library (`flnd.Run`). It starts when it is first
iterated or waited on, yields its events as they happen, and ends with a `flnd.Report`.

## Record
A saved file's row: its path, size, post and date (`flnd.Record`). `fl.files()` lists them,
from the history today.

## Home
The folder flnd keeps everything in: downloads, history, logins and settings. The library's
`flnd.open(home)` takes it; without one, it is `$FLND_HOME`, else `.flnd` in the current folder,
which is also the CLI's. Its layout is in [storage.md](storage.md).

## Login
A source's saved credentials: browser cookies for most sites, a session for Telegram. Logins are
encrypted in the home's vault (`metadata/secrets.db`, with its key in `metadata/secret.key`).

## Pace
The least time between two requests to a source's own domains, in seconds (`Source.pace`, 0.5 by
default). File downloads from a CDN aren't paced; they are limited by `Source.concurrency`.

## Stage file
A file being written: a hidden `.<name>.stage` file beside its destination, locked by the run
writing it (`.<name>.stage.lock`) and renamed onto its real name once complete. A run removes the
stale stage files an earlier, interrupted run left in the target's folder.
