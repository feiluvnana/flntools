# Glossary

The words every page of these docs uses, with what each means in flnd, and the name the code
gives it.

## Target
Something to download. It is written `<source>:<what>`, as in `ins:example_user`, or given as a
link, as in `https://www.instagram.com/example_user/`. A search is written `<source>?<words>`, as
in `mht?example`. In code, a target is `flnd.source.Target(text)`; resolving it sets its
`kind`, the name of the route that reads it, so a resolved target has its source's code, its
kind and its captured value (`example_user`).

## Source
The code for one site, as one `flnd.source.Source` subclass: a built-in one in
`flnd/sources/<code>.py`, or a plugin. Its **code** is the prefix of its targets, 2 to 16
lowercase letters and digits; the built-ins use three (`ins`, `tik`, `twt`, `fbk`, `tlg`, `loi`,
`mht`). `flnd.sources.SOURCES` lists every source, built in or a plugin. See
[writing-a-source.md](writing-a-source.md).

## Plugin
A source that isn't built in: an installed package's `flnd.sources` entry point, or a
`<code>.py` file (or `<code>/` package) in the user's plugin folder, `$FLND_SOURCES`, else
`~/.config/flnd/sources/`. Never a file in a home. See
[writing-a-source.md](writing-a-source.md#10-shipping-it-as-a-plugin).

## Route
A method of a source that reads one kind of target, made with the `@target(...)` decorator. Its
patterns say which texts and links it accepts, and its method name is the target's **kind**
(`user`, `post`, `video`, `chat`). A route either yields posts itself (a single post's link) or
returns a context for its categories (a profile). Routes are tried in the order the source
declares them, and the first whose pattern matches reads the target.

## Category
One part of a target, such as a profile's `posts`, `reels` or `stories`. It is a method made
with `@<route>.category`, and it receives what its route returned. The CLI picks
categories with `--only` (the library with `only=`); leaving it out takes every one. A single
post has no categories, and takes no `--only`.

## Query
A route whose pattern is `<code>?{words}`, such as `mht?{words}`, typed as `mht?example`. It
yields other targets instead of posts, and each one found is downloaded as if it had been typed.
A route that isn't a query but yields targets too says `discovers=True`.

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
What flnd remembers about each saved file, so a second run skips it: the `files` table of
`<home>/flnd.db`, one row per file, keyed `<code>:<key>` (see
[storage.md](storage.md)). A file's **key** comes from the source's `key` template,
`{post}_{num}` by default; changing it makes every saved file look new.

## Sync
Downloading only what is new, for followed targets. `flnd follow` follows a target, and
`flnd sync` updates them: it downloads what is new for every followed target. On a sync, a
category marked `newest_first` stops after 5 saved posts in a row (a source's `stop_after` in
`config.toml` changes it), or sooner at its head
([architecture.md](architecture.md#smart-sync)).

## Cursor
Where a paged listing got to. flnd saves each category's cursor when a run is interrupted, and the
next run resumes from it, then catches up from the newest post.

## Head
The newest post of a `newest_first` category, as the last run that read it from the top saw
it. A sync stops after the head and the post after it, so a target with nothing new costs one
page.

## Gap
Where a sync was cut short before it met the posts saved before it. The next sync, once it
meets saved posts on top, jumps to the gap's cursor and goes on there, so no post is left
behind.

## Failure
A file or target that failed and isn't saved since: one row of `flnd.db`'s `failures`, which
`flnd status` shows and the next runs retry ([errors.md](errors.md#the-failure-report)).

## Event
A report of something a run did: a file saved (`flnd.Saved`), skipped (`flnd.Skipped`) or
failed (`flnd.Failed`), the counts so far (`flnd.Progress`), or, for a dry run, a file planned
(`flnd.Planned`). A script reads a run's events by iterating it ([library.md](library.md)),
and the CLI draws its progress from the same events.

## Run
One `download`, `follow` or `sync` of the library (`flnd.Run`). It starts when it is first
iterated or waited on, yields its events as they happen, and ends with a `flnd.Report`.

## Record
A saved file's row: its path, size, post and date (`flnd.Record`). `fl.files()` lists them,
read from the history in `flnd.db`.

## Home
The folder flnd keeps everything in: downloads, history, logins and settings. The library's
`flnd.open(home)` takes it; without one, it is `$FLND_HOME`, else `.flnd` in the current folder,
which is also the CLI's. Its layout is in [storage.md](storage.md).

## Login
A source's saved credentials: browser cookies for most sites, a session for Telegram. Logins are
kept in the home's vault, `metadata/secret.db`, in plaintext
([storage.md](storage.md#the-vault)).

## Pace
The least time between two requests to a source's own domains, in seconds (`Source.pace`, 0.5 by
default). File downloads aren't paced; they are capped per host, from 8 at once up to 32 as the
host keeps up, or up to the source's `limits` for that host.

## Part
A file being written: `<name>.part` beside its destination. The part is its own lock: the run
writing it holds it, so no other run writes the same file, and it is renamed onto its real name
once whole. A failed download keeps its part, and the next run resumes it; a run removes the
parts no run holds and nothing has touched for a week. See [storage.md](storage.md).
