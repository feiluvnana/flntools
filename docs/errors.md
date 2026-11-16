# Errors

What can go wrong in a run today, which error class says so, and what to do about it.

The rebuild replaces the classes below with one hierarchy under `flnd.Error`, each error with a
`hint` that says what to do, and an exit code per class. That isn't built yet; the commit that
builds it rewrites this page.

## How a failure travels

flnd keeps a failure as small as it can:

- **One file** that fails (after its tries) is counted as failed, saved in `targets.db` with its
  request, and tried again first on the target's next run. The rest of the target goes on.
- **One target** whose listing fails ends with its error printed in red, and its
  `TargetReport.error` set. The other targets still run, and the exit code is 1.
- **One source** that can't start (its `connect` fails, or it requires a login it doesn't have)
  fails each of its targets; other sources still run.
- **A usage mistake** stops the command before anything is fetched, with exit code 2.
- **Ctrl-C** ends the run with exit code 130; see [cli.md](cli.md#ctrl-c).

Anything else (a full disk, a bug) ends the run with a Python traceback.

## What is retried

| what | where | tries |
| --- | --- | --- |
| a request to the site that couldn't be sent, or got a 5xx | `Site.fetch` | 4 in all, after about 1, 2 and 4 s |
| a 429 (rate limited) | `Site.fetch`, `Site.goto` | waited out, for the time the site asks (`Retry-After`), then tried again |
| a file download | `common/net.download` | 3 tries, each trying every mirror in order |
| a DASH video whose streams came short, or whose ffmpeg merge failed | `common/net.download` | 3 tries |

A web page or JSON where a photo or video was expected (a login wall, an error page) is never
retried: it is a failed file, named as such.

## The classes

### Raised by sources (`flnd.source`)

| class | cause | what to do |
| --- | --- | --- |
| `FetchError` | a request was refused or failed; `status` holds the HTTP status, or `None` when there was no response | read the message; a 401 or 403 usually means the login expired (`flnd auth login`) |
| `LoginError` | a login is missing, expired, or failed during `flnd auth login` | log in again with `flnd auth login` |
| `TargetError` | a target can't be read: an unknown source, a form no route accepts, or a link the source refuses on purpose | write the target in one of the forms `flnd --help` lists |
| `Error` | the base of the three above | |

`flnd` re-exports `LoginError` and `TargetError`. `flnd.Error` is the engine's own base class
today (`flnd.engine.Error`); nothing raises it yet.

### Raised by the CLI and the engine

| class | cause | exit code |
| --- | --- | --- |
| `shared.cli.UsageError` | a bad flag value, a `--from` the target doesn't have, a target not known to `flnd target`, `flnd auth login` outside a terminal | 2 |
| cyclopts' errors | an unknown command or flag, a missing argument | 2 |
| `KeyboardInterrupt` | Ctrl-C | 130 |

### Raised by the toolkit (`shared`)

Every error of the toolkit is a `shared.Error`. Each module has its own `Error` under it, and an
error about bad input is also one of four kinds, so a caller can catch by kind:

| kind | meaning | examples |
| --- | --- | --- |
| `shared.UnsafeError` | input refused because trusting it could do harm | an archive entry that escapes its folder, a shell argument that could inject |
| `shared.BrokenError` | input that is damaged or isn't what it claims | a download cut short (`http.IncompleteError`), a broken archive, unreadable HTML |
| `shared.RefusedError` | a remote end or a person said no | an HTTP 4xx (`http.RefusedError`), a wrong archive password |
| `shared.MissingError` | something this machine lacks | ffmpeg, a browser, Chrome's cookie key |

Other toolkit errors a run may show: `http.RateLimitedError` (a 429, waited out),
`http.StoppedError` (Ctrl-C during a request), `fs.BusyError` (another run holds a lock: wait,
or stop the other run), `store.Error` (a store that can't be opened) and
`common.paths.NotMediaError` (a web page instead of media).

## Common messages

| message | meaning | fix |
| --- | --- | --- |
| `--from is required (use all, or …)` | a profile, chat or search needs its categories | add `--from all`, or name the categories |
| `<target>: not logged in; run flnd auth login` | the source requires a login | `flnd auth login` |
| `<code>: not logged in, using public access` | a warning: only public posts are listed | log in to get more |
| `which source is "<text>"?` | a bare name, not a link, without a source | write it as `<code>:<text>` |
| `got a web page instead of a photo` | the site answered a file request with a page | usually a login wall: log in again |
| `held by another writer; try again later` | another flnd run is using the same home | wait for it to end |
