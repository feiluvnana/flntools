# Errors

What can go wrong in a run, which error class says so, what flnd does about it, and what you
can do.

## The classes

Every error flnd raises is a `flnd.Error`, from one hierarchy in `flnd/errors.py`:

```
flnd.Error                 str(e) says what went wrong; e.hint what to do
├── UsageError             a bad target, flag or config value
├── LoginError             not logged in, expired, or refused
├── SiteError              the site answered, but not usefully (e.status, e.response)
│   ├── Blocked            rate limited or blocked for a while (e.wait)
│   ├── NotFound           deleted, private, or never there
│   └── Changed            the answer no longer has the shape the source expects
├── NetworkError           DNS, TLS, a timeout, a reset, a transfer cut short
├── DiskError              no space, no permission, a name too long, a damaged store
├── ToolError              ffmpeg, 7zz or a browser: missing, or failed
└── SourceError            a source is broken: its declaration, or a bug in its listing

flnd.Stopped               Ctrl-C: a KeyboardInterrupt, never an Error
```

Each error has:

- **its message**, `str(error)`: one line, with no secret in it;
- **`hint`**: what to do, as a command when there is one (`run flnd login <code>`); `""` when
  there is nothing to suggest;
- **`target`**: the target it is about, as written (`ins:example_user`), or `None`;
- **`retryable`**: whether trying again later may work. Whoever raises the error sets it, from
  the policy below; the class alone doesn't decide it. A `NetworkError` for a host that doesn't
  exist won't heal, while one for a timeout may.

A `SiteError` also has `status`, the HTTP status (`None` when there was no HTTP answer), and
`response`, the answer itself, so a source can read the site's reason from its body.
`Blocked` has `wait`, the seconds the site asked to wait, or `None`.

`flnd.Stopped` derives from `KeyboardInterrupt`, so `except flnd.Error` (or `except Exception`)
never swallows a Ctrl-C.

```python
import flnd

try:
    with flnd.open() as fl:
        fl.download("zzz:example_user")
except flnd.Error as error:
    assert isinstance(error, flnd.UsageError)
    print(error)        # unknown source 'zzz'. Sources: tik, fbk, ins, twt, loi, tlg, mht

assert not issubclass(flnd.Stopped, flnd.Error)
assert issubclass(flnd.Blocked, flnd.SiteError)
```

A source imports the classes it raises from `flnd.source`: `UsageError`, `LoginError`,
`SiteError`, `Blocked`, `NotFound`, `Changed`, `NetworkError`, `SourceError`, and `Error` to
catch them all. A mistake in a source's declaration (a pattern outside its domains, a category
declared twice) raises `SourceError` when its class is made.

## The policy

| failure | class | retried | then |
| --- | --- | --- | --- |
| a timeout, a reset, a 5xx | `NetworkError`, or `SiteError` with the status | 4 tries in all, waiting about 1, 2 and 4 s (jittered) | the file fails alone; the run goes on |
| a 429 or a block page | `Blocked` | waits what the site asks (`Retry-After`, or its rate-limit reset, up to 1 h), within the same 4 tries | the file or target fails |
| a 404 or 410, a private item | `NotFound` | never | the file or target fails |
| a 401 or 403 from the source's own domains, a login wall | `LoginError` | never | the source stops: its other targets fail with the same error, and the other sources go on |
| a 401 or 403 from another host (a CDN's expired link) | `SiteError` | a 403 or 410 on a post whose files the source lists lazily lists them again once, for a fresh link | the file fails alone |
| a truncated transfer | `NetworkError` | yes, resuming from the part | the part is kept for the next run |
| an answer that doesn't parse, a web page where media was expected | `Changed`, `SiteError` | never | the file or target fails |
| no space, no permission, a name too long | `DiskError` | never | the run stops |
| a missing or failing ffmpeg, 7zz or browser | `ToolError` | a DASH merge that ffmpeg failed is tried 3 times in all; a missing tool never | the file or target fails |
| an unexpected exception in a listing | `SourceError` | never | the target fails, and its message says it is a bug |

What "fails" means at each level:

- **A file** that fails (after its tries) is counted as failed, written to `flnd.db`'s
  `failures` with its request, and tried again first on the target's next run. A 404 or 410
  (`NotFound`) records the file as gone instead: it isn't asked for again unless told to
  (`--again`). The rest of the target goes on.
- **A target** whose listing fails ends with its error printed, then its hint on the next line,
  and `TargetReport.error` holds the error. The other targets still run.
- **A source** whose login fails, or that can't start (its `connect` fails), fails each of its
  targets; the other sources still run.
- **A usage mistake** stops the command before anything is fetched.
- **A full or refused disk** stops the run; the files already saved keep their rows.

The run's exit code says which happened; [cli.md](cli.md#exit-codes) lists the codes: 1 for
failed files or targets, 2 for usage, 3 for a login, 4 when flnd itself stopped, 130 for Ctrl-C.

## No tracebacks for a person's mistake

A `UsageError`, `LoginError`, `DiskError` or `ToolError` prints one line saying what went wrong
and one line saying what to do:

```
x ins:example_user: not logged in
• ins:example_user: run flnd login ins
```

An unexpected exception is a bug. The CLI prints one line naming a crash file,
`.flnd/logs/crash-<time>.txt`, which holds the traceback, and exits 4; with `-v` the traceback
prints instead. A bug inside one target's listing doesn't stop the run: it becomes that target's
`SourceError`, and its cause is printed with `-v`.

## Secrets never reach a message

- A URL in a message is redacted: its user info, query and fragment are dropped.
- The secret headers a request carried (`Authorization`, `Cookie`, `Proxy-Authorization`), their
  parts (a cookie's value, a bearer token) and the URL's query values are replaced by `***`
  wherever a library's error text quotes them.
- A cookie's value is never printed, logged or put in an error, even when a cookie file is
  broken: the message names the file and the line.

## Not built yet

- The `failures` table in `flnd.db`, `flnd status` and `flnd retry`, and the short failure
  summary at the end of a run.
- A sample of a response that didn't parse, saved scrubbed to `logs/<run>/samples/`.
- One cookie refresh from the browser before a 401 or 403 becomes a `LoginError`.
- Recording a `NotFound` item as gone, so a later run doesn't ask again.
- The third stage of Ctrl-C; today the first stops listing and finishes the files in progress,
  and the second stops at once ([cli.md](cli.md#ctrl-c)).

## Common messages

| message | meaning | fix |
| --- | --- | --- |
| `--only applies to profiles and chats; <target> is one post` | a single post has no categories | leave `--only` out |
| `<target>: not logged in` | the source requires a login | `flnd login <code>` |
| `<code>: not logged in, using public access` | a warning: only public posts are listed | log in to get more |
| `<code>: the site refused the login (HTTP 403)` | the saved login expired or was revoked | `flnd login <code>` |
| `which source is "<text>"?` | a bare name, not a link, without a source | write it as `<code>:<text>` |
| `got a web page instead of a photo` | the site answered a file request with a page | usually a login wall: log in again |
| `held by another writer` | another flnd run is using the same home | wait for it to end |
| `unexpected <Type>: …` | a bug in a source or in flnd | run with `-v` and report it |
